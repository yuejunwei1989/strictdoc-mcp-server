#!/usr/bin/env python3
"""sync_polarion.py -- one-way sync of Polarion work items into StrictDoc.

Deterministic pipeline (no LLM involved):

    Polarion REST (2023 flavor) --> docs/polarion/<PROJECT>.sdoc + sync_report.json

Mapping rules
-------------
- UID       : Polarion work item id, used verbatim (stable across runs).
- TITLE     : attributes.title (single line).
- STATEMENT : attributes.description, HTML stripped to plain text.
- STATUS    : attributes.status, optionally remapped via --status-map.
- RELATIONS : outgoing links whose role is in --parent-roles become
              `RELATIONS: - TYPE: Parent` (StrictDoc: child declares parent).
- COMMENT   : provenance ("Polarion: <id> updated=<ts>"), custom fields,
              and all non-parent links (role kept verbatim).

Usage
-----
    export POLARION_URL=https://polarion.example.com
    export POLARION_USERNAME=alice POLARION_PASSWORD=secret
    python sync_polarion.py --project DEMO \
        --query "type:requirement" \
        --out-dir docs/polarion \
        --status-map open=draft,inProgress=draft,closed=approved \
        --parent-roles parent,derives

CI usage: `--fail-on-changes` exits 1 when the generated documents differ
from the previous run, so drift between Polarion and the repo is visible.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import requests

# --------------------------------------------------------------------- html


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0
        self._block = False

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self._SKIP:
            self._skip += 1
        elif tag in ("p", "br", "li", "tr", "div", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip and data.strip():
            self.parts.append(data)


def html_to_text(html: str | None) -> str:
    if not html:
        return ""
    p = _TextExtractor()
    p.feed(html)
    text = "".join(p.parts)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def one_line(text: str | None, limit: int = 200) -> str:
    return " ".join((text or "").split())[:limit]


# ------------------------------------------------------------------- client


class PolarionClient:
    """Minimal read-only Polarion REST (JSON:API) client, 2023-compatible."""

    def __init__(self, url: str, timeout: float = 30.0, verify_ssl: bool = True):
        base = url.rstrip("/")
        # Accept https://host, https://host/polarion or .../rest/v1 and
        # normalize to the REST root.
        if base.endswith("/rest/v1"):
            pass
        elif base.endswith("/polarion"):
            base += "/rest/v1"
        else:
            base += "/polarion/rest/v1"
        self.base = base
        self.timeout = timeout
        self.session = requests.Session()
        self.session.verify = verify_ssl
        token = os.environ.get("POLARION_TOKEN")
        user, password = os.environ.get("POLARION_USERNAME"), os.environ.get("POLARION_PASSWORD")
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        elif user and password:
            self.session.auth = (user, password)
        else:
            sys.exit("error: set POLARION_TOKEN or POLARION_USERNAME/PASSWORD")

    def get(self, path: str, params: dict | None = None) -> dict:
        r = self.session.get(f"{self.base}/{path}", params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def iter_workitems(self, project: str, query: str | None, page_size: int = 100):
        # Polarion REST pagination is 1-based.
        page = 1
        while True:
            params: dict[str, Any] = {
                "fields[workitems]": "@all",
                "page[size]": page_size,
                "page[number]": page,
            }
            if query and query.strip():
                params["query"] = query.strip()
            data = self.get(f"projects/{project}/workitems", params=params)
            items = (data.get("data") or [])
            yield from items
            total = ((data.get("meta") or {}).get("totalCount"))
            page += 1
            if not items or (total is not None and page * page_size >= int(total)):
                return

    def links(self, project: str, work_item_id: str) -> list[dict]:
        try:
            data = self.get(f"projects/{project}/workitems/{work_item_id}/linkedworkitems")
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return []
            raise
        return data.get("data") or []


def _link_attr(link: dict, key: str) -> str | None:
    v = (link.get("attributes") or {}).get(key)
    if isinstance(v, dict):
        return v.get("id") or v.get("name")
    return v


def _link_target(link: dict) -> str | None:
    rel = ((link.get("relationships") or {}).get("target") or {})
    return ((rel.get("data") or {}) or {}).get("id")


# ---------------------------------------------------------------- generator


def sdoc_escape(text: str) -> str:
    return text.replace(">>>", "＞＞＞")


def build_sdoc(project: str, workitems: list[dict], links_by_id: dict[str, list[dict]],
               status_map: dict[str, str], parent_roles: set[str]) -> str:
    lines = [
        "[DOCUMENT]",
        f"TITLE: Polarion Requirements (project {project})",
        f"UID: POL-{project}",
        "",
        "[REQUIREMENT]",
        "UID: SYNC-META",
        "TITLE: 同步说明",
        "STATEMENT: >>>",
        f"本文件由 sync_polarion.py 从 Polarion 项目 {project} 自动生成，单向同步，"
        "请勿手工编辑；修改请回到 Polarion 后重新同步。",
        "<<<",
        "",
    ]
    for wi in sorted(workitems, key=lambda w: w.get("id") or ""):
        wid = wi.get("id") or ""
        attrs = wi.get("attributes") or {}
        raw_status = str(attrs.get("status") or "")
        status = status_map.get(raw_status, raw_status)
        statement = html_to_text(attrs.get("description")) or one_line(attrs.get("title"))

        # Grammar order: UID, LEVEL, STATUS, TAGS, TITLE, STATEMENT,
        # RATIONALE, COMMENT, RELATIONS.
        lines += [
            "[REQUIREMENT]",
            f"UID: {wid}",
        ]
        if status:
            lines.append(f"STATUS: {status}")
        lines.append(f"TITLE: {one_line(attrs.get('title')) or wid}")
        lines += ["STATEMENT: >>>", sdoc_escape(statement) or "(empty)", "<<<"]

        comment: list[str] = [
            f"Polarion: {wid} type={attrs.get('type')} updated={attrs.get('updated')}",
        ]
        for key, val in sorted((attrs or {}).items()):
            if key in ("title", "status", "description", "type", "created", "updated"):
                continue
            comment.append(f"{key}: {one_line(json.dumps(val, ensure_ascii=False), 120)}")

        parent_links, other_links = [], []
        for link in links_by_id.get(wid, []):
            role = _link_attr(link, "role") or "?"
            target = _link_target(link)
            entry = f"{role} -> {target}" + (
                " [SUSPECT]" if _link_attr(link, "suspect") else ""
            )
            (parent_links if role in parent_roles else other_links).append(entry)
        if other_links:
            comment.append("links: " + "; ".join(other_links))

        # SDoc field order matters: COMMENT before RELATIONS, and no blank
        # line between the COMMENT block and RELATIONS.
        lines += ["COMMENT: >>>", *comment, "<<<"]

        if parent_links:
            lines.append("RELATIONS:")
            for entry in parent_links:
                lines.append("- TYPE: Parent")
                lines.append(f"  VALUE: {entry.split('->')[-1].strip()}")
        lines.append("")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", default=os.environ.get("POLARION_DEFAULT_PROJECT"))
    ap.add_argument("--query", default="type:requirement", help="Lucene query")
    ap.add_argument("--out-dir", default="docs/polarion")
    ap.add_argument("--status-map", default="",
                    help="comma list polarion_status=sdoc_status, e.g. open=draft,closed=approved")
    ap.add_argument("--parent-roles", default="parent,derives",
                    help="link roles mapped to StrictDoc Parent relations")
    ap.add_argument("--fail-on-changes", action="store_true",
                    help="exit 1 when output differs from the previous run (CI gate)")
    args = ap.parse_args()
    if not args.project:
        ap.error("--project or POLARION_DEFAULT_PROJECT is required")

    status_map = dict(
        part.split("=", 1) for part in args.status_map.split(",") if "=" in part
    )
    parent_roles = {r.strip() for r in args.parent_roles.split(",") if r.strip()}

    client = PolarionClient(os.environ["POLARION_URL"],
                            verify_ssl=os.environ.get("POLARION_VERIFY_SSL", "true").lower() != "false")

    workitems = list(client.iter_workitems(args.project, args.query))
    links_by_id: dict[str, list[dict]] = {
        wi.get("id") or "": client.links(args.project, wi.get("id") or "")
        for wi in workitems
    }
    sdoc = build_sdoc(args.project, workitems, links_by_id, status_map, parent_roles)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{args.project}.sdoc"
    prev = out_file.read_text(encoding="utf-8") if out_file.exists() else ""

    digest = hashlib.sha256(sdoc.encode("utf-8")).hexdigest()[:16]
    changed = sdoc != prev
    out_file.write_text(sdoc, encoding="utf-8")

    report = {
        "project": args.project,
        "query": args.query,
        "work_items": len(workitems),
        "ids": sorted(wi.get("id") or "" for wi in workitems),
        "output": str(out_file),
        "content_sha256_16": digest,
        "changed_vs_previous_run": changed,
    }
    report_path = out_dir / "sync_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if (args.fail_on_changes and changed) else 0


if __name__ == "__main__":
    sys.exit(main())
