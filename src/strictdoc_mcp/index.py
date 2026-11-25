"""In-memory index over a StrictDoc JSON export.

The server is READ-ONLY: it parses ``index.json`` produced by::

    strictdoc export <docs> --formats=json --output-dir <out>

and never writes to the document tree.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

RELATION_KEYS = {"TYPE", "VALUE", "ROLE"}


class StrictDocIndex:
    """Parsed view of a StrictDoc JSON export."""

    def __init__(self, json_path: str | Path):
        self.json_path = Path(json_path)
        raw = json.loads(self.json_path.read_text(encoding="utf-8"))
        self.raw_mtime = self.json_path.stat().st_mtime

        self.documents: list[dict[str, Any]] = raw.get("DOCUMENTS", [])
        # uid -> node (only nodes carrying a UID)
        self.nodes: dict[str, dict[str, Any]] = {}
        # uid -> owning document uid
        self.node_doc: dict[str, str] = {}
        # uid -> section breadcrumb (titles, top-down)
        self.node_path: dict[str, list[str]] = {}
        # edges: (src_uid, type, role, dst_uid) -- relations DECLARED by src,
        # pointing at dst. In StrictDoc a child declares `Parent -> upstream`,
        # so declared targets are the UPSTREAM direction.
        self.edges: list[tuple[str, str, str | None, str]] = []
        self.section_count = 0

        for doc in self.documents:
            doc_uid = doc.get("UID") or doc.get("TITLE", "untitled")
            self._walk(doc, doc_uid, [])

    # ------------------------------------------------------------------ parse

    def _walk(self, node: dict[str, Any], doc_uid: str, path: list[str]) -> None:
        node_type = node.get("_NODE_TYPE", "")
        title = (node.get("TITLE") or "").strip()
        uid = node.get("UID")

        if uid:
            self.nodes[uid] = node
            self.node_doc[uid] = doc_uid
            self.node_path[uid] = list(path)

        if node_type == "DOCUMENT":
            child_path: list[str] = []
        elif node_type == "SECTION":
            child_path = path + ([title] if title else [])
        else:
            child_path = path

        if node_type == "SECTION":
            self.section_count += 1

        for rel in node.get("RELATIONS") or []:
            if not isinstance(rel, dict):
                continue
            target = rel.get("VALUE")
            if not target:
                continue
            self.edges.append(
                (uid or "", rel.get("TYPE", "?"), rel.get("ROLE"), str(target))
            )

        for child in node.get("NODES") or []:
            self._walk(child, doc_uid, child_path)

    # ----------------------------------------------------------------- views

    def summary(self) -> dict[str, Any]:
        reqs = [
            n for n in self.nodes.values() if n.get("_NODE_TYPE") == "REQUIREMENT"
        ]
        by_status: dict[str, int] = {}
        for r in reqs:
            s = (r.get("STATUS") or "UNSET").strip()
            by_status[s] = by_status.get(s, 0) + 1
        return {
            "json_path": str(self.json_path),
            "documents": len(self.documents),
            "requirements": len(reqs),
            "sections": self.section_count,
            "requirements_by_status": by_status,
            "relations": len(self.edges),
            "unresolved_relations": sum(
                1 for *_a, dst in self.edges if dst not in self.nodes
            ),
        }

    def list_documents(self) -> list[dict[str, Any]]:
        out = []
        for doc in self.documents:
            doc_uid = doc.get("UID") or doc.get("TITLE", "untitled")
            reqs: list[str] = []
            sections: list[str] = []
            self._collect(doc, reqs, sections)
            out.append(
                {
                    "uid": doc_uid,
                    "title": doc.get("TITLE"),
                    "requirements": reqs,
                    "num_requirements": len(reqs),
                    "num_sections": len(sections),
                }
            )
        return out

    def _collect(self, node: dict, reqs: list[str], sections: list[str]) -> None:
        t = node.get("_NODE_TYPE")
        if t == "REQUIREMENT" and node.get("UID"):
            reqs.append(node["UID"])
        elif t == "SECTION" and node.get("UID"):
            sections.append(node["UID"])
        for c in node.get("NODES") or []:
            self._collect(c, reqs, sections)

    def document_tree(self, doc_uid: str) -> dict[str, Any]:
        for doc in self.documents:
            if (doc.get("UID") or "") == doc_uid:
                return self._tree(doc)
        raise KeyError(f"document not found: {doc_uid}")

    def _tree(self, node: dict) -> dict[str, Any]:
        out: dict[str, Any] = {
            "type": node.get("_NODE_TYPE"),
            "title": node.get("TITLE"),
        }
        if node.get("UID"):
            out["uid"] = node["UID"]
        if node.get("STATUS"):
            out["status"] = node["STATUS"]
        kids = [self._tree(c) for c in node.get("NODES") or []]
        if kids:
            out["children"] = kids
        return out

    # ------------------------------------------------------------ requirement

    def source_ref(self, uid: str) -> str:
        doc = self.node_doc.get(uid, "?")
        return f"{doc}.sdoc#UID={uid}"

    def _rel_view(self, owner: str, rel_type: str, role: str | None, peer: str,
                  direction: str) -> dict[str, Any]:
        """View of one relation from the perspective of `owner`; `peer` is the
        other endpoint (declared target for 'declared', referencer for
        'referenced_by')."""
        target = self.nodes.get(peer)
        view: dict[str, Any] = {
            "direction": direction,
            "type": rel_type,
            "uid": peer,
        }
        if role:
            view["role"] = role
        if target is None:
            view["resolved"] = False
        else:
            view["resolved"] = True
            view["title"] = target.get("TITLE")
            view["status"] = (target.get("STATUS") or "").strip() or None
            view["source"] = self.source_ref(peer)
        if not owner:
            view["note"] = "relation declared on a node without UID"
        return view

    def get_requirement(self, uid: str) -> dict[str, Any]:
        node = self.nodes[uid]
        fields = {
            k: v
            for k, v in node.items()
            if not k.startswith("_")
            and k not in ("UID", "TITLE", "NODES", "RELATIONS", "GRAMMAR")
            and v not in (None, "")
        }
        # 'declared': relations this node declares itself (typically
        # `Parent -> upstream`); 'referenced_by': other nodes pointing at uid.
        declared = [
            self._rel_view(uid, t, role, dst, "declared")
            for s, t, role, dst in self.edges
            if s == uid
        ]
        referenced_by = [
            self._rel_view(dst, t, role, s, "referenced_by")
            for s, t, role, dst in self.edges
            if dst == uid and s
        ]
        return {
            "uid": uid,
            "node_type": node.get("_NODE_TYPE"),
            "title": node.get("TITLE"),
            "document": self.node_doc.get(uid),
            "section_path": self.node_path.get(uid, []),
            "fields": fields,
            "relations": {"declared": declared, "referenced_by": referenced_by},
            "source": self.source_ref(uid),
        }

    # ----------------------------------------------------------- traceability

    def traceability(self, uid: str, max_depth: int = 5) -> dict[str, Any]:
        # upstream: follow declared targets (reverse=False); downstream:
        # follow nodes that reference uid (reverse=True).
        upstream = self._traverse(uid, reverse=False, max_depth=max_depth)
        downstream = self._traverse(uid, reverse=True, max_depth=max_depth)
        return {
            "uid": uid,
            "title": self.nodes[uid].get("TITLE"),
            "source": self.source_ref(uid),
            "upstream": upstream,
            "downstream": downstream,
        }

    def _traverse(self, uid: str, reverse: bool, max_depth: int) -> list[dict]:
        """Collect chains. Forward (upstream): follow the targets this node's
        relations declare (Parent -> upstream). Reverse (downstream): follow
        nodes that declare a relation pointing at the current node."""
        chains: list[dict] = []
        visited: set[str] = {uid}

        def step(cur: str, path: list[str], depth: int) -> None:
            nxt: list[str] = []
            for s, _t, _r, dst in self.edges:
                if not reverse and s == cur and dst not in visited:
                    nxt.append(dst)
                elif reverse and dst == cur and s and s not in visited:
                    nxt.append(s)
            for n in nxt:
                node = self.nodes.get(n)
                chain = path + [
                    f"{n}: {node.get('TITLE')}" if node else f"{n}: UNRESOLVED"
                ]
                chains.append(
                    {
                        "depth": depth,
                        "chain": chain,
                        "source": self.source_ref(n) if node else None,
                    }
                )
                if depth < max_depth:
                    visited.add(n)
                    step(n, chain, depth + 1)

        step(uid, [], 1)
        chains.sort(key=lambda c: (c["depth"], c["chain"]))
        return chains

    # ----------------------------------------------------------------- search

    def search(
        self,
        query: str = "",
        status: str | None = None,
        doc_uid: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        q = query.strip().lower()
        hits: list[dict[str, Any]] = []
        for uid, node in self.nodes.items():
            if node.get("_NODE_TYPE") != "REQUIREMENT":
                continue
            if status and (node.get("STATUS") or "").strip().lower() != status.lower():
                continue
            if doc_uid and self.node_doc.get(uid) != doc_uid:
                continue
            if q:
                hay = " ".join(
                    str(node.get(k, ""))
                    for k in ("UID", "TITLE", "STATEMENT", "RATIONALE", "COMMENT", "TAGS")
                ).lower()
                if q not in hay:
                    continue
            hits.append(
                {
                    "uid": uid,
                    "title": node.get("TITLE"),
                    "status": (node.get("STATUS") or "").strip() or None,
                    "document": self.node_doc.get(uid),
                    "section_path": self.node_path.get(uid, []),
                    "source": self.source_ref(uid),
                }
            )
            if len(hits) >= limit:
                break
        return hits

    # ----------------------------------------------------------------- orphans

    def find_orphans(self) -> dict[str, list[dict]]:
        """Gap analysis.
        - no_upstream: requirement declares no relation at all (nothing above
          it in the traceability chain -- usually only acceptable at the very
          top level of the V-model).
        - no_downstream: no other node declares a relation pointing at it
          (requirement not allocated/implemented/verified by anything)."""
        declared_uids = {s for s, *_rest in self.edges if s}
        referenced_uids = {dst for *_s, dst in self.edges}
        no_up: list[dict] = []
        no_down: list[dict] = []
        for uid, node in self.nodes.items():
            if node.get("_NODE_TYPE") != "REQUIREMENT":
                continue
            info = {
                "uid": uid,
                "title": node.get("TITLE"),
                "status": (node.get("STATUS") or "").strip() or None,
                "document": self.node_doc.get(uid),
                "source": self.source_ref(uid),
            }
            if uid not in declared_uids:
                no_up.append(dict(info, meaning="declares no upstream relation"))
            if uid not in referenced_uids:
                no_down.append(dict(info, meaning="nothing downstream references it"))
        return {"no_upstream": no_up, "no_downstream": no_down}


class ReloadableIndex:
    """Reloads the index when the JSON file changes on disk."""

    def __init__(self, json_path: str | Path):
        self.json_path = Path(json_path)
        if not self.json_path.is_file():
            raise FileNotFoundError(
                f"StrictDoc JSON export not found: {self.json_path} "
                "(generate with: strictdoc export <docs> --formats=json)"
            )
        self._lock = threading.Lock()
        self._index = StrictDocIndex(self.json_path)

    def get(self) -> StrictDocIndex:
        with self._lock:
            try:
                mtime = self.json_path.stat().st_mtime
                if mtime != self._index.raw_mtime:
                    self._index = StrictDocIndex(self.json_path)
            except OSError:
                pass
            return self._index


def resolve_json_path() -> Path:
    """CLI arg > env var > ./output/json/index.json > ./json/index.json."""
    import sys

    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    candidates: list[Path] = []
    if args:
        candidates.append(Path(args[0]))
    env = os.environ.get("STRICTDOC_MCP_JSON")
    if env:
        candidates.append(Path(env))
    candidates += [
        Path("output/json/index.json"),
        Path("json/index.json"),
    ]
    for c in candidates:
        if c.is_file():
            return c
    raise FileNotFoundError(
        "No StrictDoc JSON export found. Pass the path to index.json as the first "
        "CLI argument, set STRICTDOC_MCP_JSON, or run "
        "'strictdoc export <docs> --formats=json --output-dir output' first."
    )
