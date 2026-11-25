#!/usr/bin/env python3
"""sync_davinci_arxml.py -- one-way sync of a DaVinci (AUTOSAR SWC) ARXML
architecture into StrictDoc.

Deterministic pipeline (no LLM involved):

    SWC / Composition ARXML ──sync_davinci_arxml.py──> docs/davinci/<NAME>.sdoc

The ARXML design file is the authoritative architecture model; DaVinci CLI
(dvdevc / dvcfg-b) is NOT needed for this step -- we parse the model directly,
read-only. Use the davinci-mcp-server for validation and code generation.

Mapping (SWE.2, per V-model layers)
-----------------------------------
| ARXML element                          | StrictDoc                     |
|----------------------------------------|-------------------------------|
| COMPOSITION-SW-COMPONENT-TYPE          | ARCH-<path> (architecture)    |
| SW-COMPONENT-TYPE                      | ARCH-<path>, Parent -> owning |
|                                        | composition                   |
| P-/R-PORT-PROTOTYPE + interface ops    | COMMENT of the SWC (port      |
|                                        | table), interface TREFs       |
| ASSEMBLY-SW-CONNECTOR-PROTOTYPE        | COMMENT of the composition    |
| RUNNABLE-ENTITY                        | DES-<symbol>, Parent -> SWC   |
| TIMING-EVENT / OPERATION-INVOKED-EVENT | COMMENT of the runnable       |

The RUNNABLE `SYMBOL` is the anchor to the C code: the generated DES-<symbol>
UID is stable and can be matched against Doxygen symbols / @req tags.

UID strategy: logical SHORT-NAME path slugged (`/WindowMgmt/AntiPinch` ->
`windowmgmt-antipinch`), NOT physical file paths -- renaming a file must not
change the UID (otherwise every re-import creates all-new items).

Usage
-----
    python sync_davinci_arxml.py --arxml design.arxml --name WindowMgmt \
        --out-dir docs/davinci
    # CI gate:
    python sync_davinci_arxml.py ... --fail-on-changes
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

from lxml import etree

# ------------------------------------------------------------------ helpers


def slug(text: str) -> str:
    """Lowercase, keep [a-z0-9-], collapse separators. Stable UID material."""
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip()).strip("-").lower()
    return s or "unnamed"


def _local(tag: Any) -> str:
    """Strip the XML namespace: '{http://autosar.org/schema/r4.0}X' -> 'X'."""
    t = tag if isinstance(tag, str) else ""
    return t.rsplit("}", 1)[-1]


def _children(el: Any, name: str) -> list[Any]:
    return [c for c in el if _local(c.tag) == name]


def _child(el: Any, name: str) -> Any | None:
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _text(el: Any, name: str) -> str:
    c = _child(el, name)
    return (c.text or "").strip() if c is not None else ""


def _ref(el: Any, name: str) -> str:
    """VALUE of a <NAME-REF ...>/...-REF child, DEST attribute kept aside."""
    c = _child(el, name)
    return (c.text or "").strip() if c is not None else ""


# ------------------------------------------------------------------ parsing


class ArxmlModel:
    """Flattened, namespace-agnostic view of a SWC-design ARXML."""

    def __init__(self, path: str | Path):
        tree = etree.parse(str(path))
        root = tree.getroot()

        self.compositions: list[dict[str, Any]] = []
        self.components: list[dict[str, Any]] = []
        self.interfaces: list[dict[str, Any]] = []
        # Collect during the package walk: lxml element proxies are transient,
        # so we must NOT index elements by id() across traversals.
        self._walk_packages(root, "")

    # ------------------------------------------------------------------ read

    def _walk_packages(self, el: Any, prefix: str) -> None:
        """Dispatch collection while walking AR-PACKAGES, carrying the
        SHORT-NAME package path (stable logical path) down to each element."""
        for c in el:
            tag = _local(c.tag)
            if tag == "AR-PACKAGES":
                self._walk_packages(c, prefix)
            elif tag == "AR-PACKAGE":
                name = _text(c, "SHORT-NAME")
                path = f"{prefix}/{name}" if prefix else f"/{name}"
                elements = _child(c, "ELEMENTS")
                if elements is not None:
                    for item in elements:
                        self._collect_one(item, path)
                self._walk_packages(c, path)

    def _collect_one(self, el: Any, pkg: str) -> None:
        tag = _local(el.tag)
        if tag == "COMPOSITION-SW-COMPONENT-TYPE":
            self.compositions.append(self._read_composition(el, pkg))
        elif tag in ("SW-COMPONENT-TYPE", "SERVICE-SW-COMPONENT-TYPE",
                     "ECU-ABSTRACTION-SW-COMPONENT-TYPE",
                     "SENSOR-ACTUATOR-SW-COMPONENT-TYPE"):
            self.components.append(self._read_component(el, pkg))
        elif tag in ("SENDER-RECEIVER-INTERFACE", "CLIENT-SERVER-INTERFACE"):
            self.interfaces.append(self._read_interface(el, tag, pkg))
        elif tag == "PORT-INTERFACE":
            # DaVinci exports wrap interfaces in a generic PORT-INTERFACE
            # element; sniff the kind from its children.
            if _child(el, "OPERATIONS") is not None:
                self.interfaces.append(
                    self._read_interface(el, "CLIENT-SERVER-INTERFACE", pkg))
            elif _child(el, "DATA-ELEMENTS") is not None:
                self.interfaces.append(
                    self._read_interface(el, "SENDER-RECEIVER-INTERFACE", pkg))

    def _read_composition(self, el: Any, pkg: str) -> dict[str, Any]:
        name = _text(el, "SHORT-NAME")
        path = f"{pkg}/{name}"
        comp = {
            "path": path,
            "name": name,
            "prototypes": [],
            "connectors": [],
        }
        comps = _child(el, "COMPONENTS")
        for proto in (_children(comps, "COMPONENT-PROTOTYPE") if comps is not None else []):
            comp["prototypes"].append(
                {
                    "name": _text(proto, "SHORT-NAME"),
                    "type": _ref(proto, "TYPE-TREF"),
                }
            )
        conns = _child(el, "ASSEMBLY-SW-CONNECTORS")
        for c in (_children(conns, "ASSEMBLY-SW-CONNECTOR-PROTOTYPE")
                  if conns is not None else []):
            comp["connectors"].append(self._read_connector(c))
        return comp

    def _read_connector(self, c: Any) -> dict[str, Any]:
        def end(parent_tag: str, port_tag: str) -> str:
            iref = _child(c, parent_tag)
            if iref is None:
                return "?"
            ctx = _child(iref, "CONTEXT-COMPONENT-PROTOTYPE")
            comp_ref = _ref(ctx, "COMPONENT-REF") if ctx is not None else ""
            port_ref = _ref(iref, port_tag)
            comp_name = comp_ref.rsplit("/", 1)[-1] if comp_ref else "?"
            port_name = port_ref.rsplit("/", 1)[-1] if port_ref else "?"
            return f"{comp_name}.{port_name}"

        return {
            "name": _text(c, "SHORT-NAME"),
            "provider": end("PROVIDER-IREF", "TARGET-P-PORT-REF"),
            "requester": end("REQUESTER-IREF", "TARGET-R-PORT-REF"),
        }

    def _read_component(self, el: Any, pkg: str) -> dict[str, Any]:
        name = _text(el, "SHORT-NAME")
        path = f"{pkg}/{name}" if pkg else f"/{name}"
        comp = {
            "path": path,
            "name": name,
            "kind": _local(el.tag),
            "ports": [],
            "runnables": [],
        }
        ports = _child(el, "PORTS")
        port_list = (_children(ports, "P-PORT-PROTOTYPE")
                     + _children(ports, "R-PORT-PROTOTYPE")) if ports is not None else []
        for port in port_list:
            ptag = _local(port.tag)
            itag = ("PROVIDED-INTERFACE-TREF" if ptag.startswith("P-")
                    else "REQUIRED-INTERFACE-TREF")
            comp["ports"].append(
                {
                    "direction": "P" if ptag.startswith("P-") else "R",
                    "name": _text(port, "SHORT-NAME"),
                    "interface": _ref(port, itag),
                }
            )
        for ib in root_or(el, "INTERNAL-BEHAVIORS", "INTERNAL-BEHAVIOR"):
            for ev_wrap in ("EVENTS",):
                events_el = _child(ib, ev_wrap)
                events = events_el if events_el is not None else []
                ev_by_runnable: dict[str, list[str]] = {}
                for ev in events:
                    etag = _local(ev.tag)
                    target = _ref(ev, "START-ON-EVENT-REF")
                    if not target:
                        continue
                    rname = target.rsplit("/", 1)[-1]
                    if etag == "TIMING-EVENT":
                        period = _text(ev, "PERIOD")
                        ev_by_runnable.setdefault(rname, []).append(
                            f"cyclic {period}s" if period else "cyclic"
                        )
                    elif etag == "OPERATION-INVOKED-EVENT":
                        ev_by_runnable.setdefault(rname, []).append("operation-invoked")
                    elif etag == "DATA-RECEIVED-EVENT":
                        ev_by_runnable.setdefault(rname, []).append("data-received")
                runnables = _child(ib, "RUNNABLES")
                for r in (_children(runnables, "RUNNABLE-ENTITY")
                          if runnables is not None else []):
                    rname = _text(r, "SHORT-NAME")
                    comp["runnables"].append(
                        {
                            "name": rname,
                            "symbol": _text(r, "SYMBOL"),
                            "path": f"{path}/ib/{rname}",
                            "events": ev_by_runnable.get(rname, []),
                        }
                    )
        return comp

    def _read_interface(self, el: Any, tag: str, pkg: str) -> dict[str, Any]:
        name = _text(el, "SHORT-NAME")
        path = f"{pkg}/{name}" if pkg else f"/{name}"
        iface = {"path": path, "name": name, "kind": tag, "members": []}
        if tag == "SENDER-RECEIVER-INTERFACE":
            container, member_tag = "DATA-ELEMENTS", "VARIABLE-DATA-PROTOTYPE"
        else:
            container, member_tag = "OPERATIONS", "CLIENT-SERVER-OPERATION"
        container_el = _child(el, container)
        for m in _children(container_el if container_el is not None else [], member_tag):
            iface["members"].append(_text(m, "SHORT-NAME"))
        return iface


def root_or(el: Any, wrapper: str, inner: str) -> list[Any]:
    w = _child(el, wrapper)
    return _children(w, inner) if w is not None else []


# ---------------------------------------------------------------- generation


def sdoc_escape(text: str) -> str:
    return text.replace(">>>", "＞＞＞")


def uid_for(prefix: str, path: str) -> str:
    return f"{prefix}-{slug(path.lstrip('/'))}"


def build_sdoc(model: ArxmlModel, name: str) -> str:
    lines = [
        "[DOCUMENT]",
        f"TITLE: Software Architecture ({name})",
        f"UID: SAD-{slug(name)}",
        "",
        "[REQUIREMENT]",
        "UID: SYNC-META-ARCH",
        "TITLE: 同步说明",
        "STATEMENT: >>>",
        f"本文件由 sync_davinci_arxml.py 从 ARXML（{name}）自动生成，单向同步，"
        "请勿手工编辑；架构修改请在 DaVinci Developer 中进行后重新同步。",
        "<<<",
        "",
    ]

    iface_by_path = {i["path"]: i for i in model.interfaces}

    def port_lines(comp: dict) -> list[str]:
        out = []
        for p in comp["ports"]:
            iface = iface_by_path.get(p["interface"])
            members = f" ({', '.join(iface['members'])})" if iface else ""
            out.append(f"Port[{p['direction']}] {p['name']} -> {p['interface']}{members}")
        return out

    # Compositions first (parents), then components, then runnables.
    for c in model.compositions:
        uid = uid_for("ARCH", c["path"])
        conn_lines = [
            f"{cn['name']}: {cn['provider']} => {cn['requester']}"
            for cn in c["connectors"]
        ]
        proto_lines = [
            f"contains {p['name']} ({p['type']})" for p in c["prototypes"]
        ]
        comment = [f"ARXML: {c['path']} (composition)"]
        comment += proto_lines + conn_lines
        lines += [
            "[REQUIREMENT]",
            f"UID: {uid}",
            f"TITLE: 组合组件 {c['name']}",
            "STATEMENT: >>>",
            sdoc_escape(
                f"{c['name']} 组合 {len(c['prototypes'])} 个组件，"
                f"{len(c['connectors'])} 条装配连接。"
            ),
            "<<<",
            "COMMENT: >>>",
            *comment,
            "<<<",
            "",
        ]

    for c in model.components:
        uid = uid_for("ARCH", c["path"])
        # find owning composition via prototype type ref
        owner = ""
        for comp in model.compositions:
            for p in comp["prototypes"]:
                if p["type"] == c["path"]:
                    owner = uid_for("ARCH", comp["path"])
        lines += [
            "[REQUIREMENT]",
            f"UID: {uid}",
            f"TITLE: 组件 {c['name']}",
            "STATEMENT: >>>",
            sdoc_escape(
                f"{c['name']}（{c['kind']}）：{len(c['ports'])} 个端口，"
                f"{len(c['runnables'])} 个可运行实体。"
            ),
            "<<<",
            "COMMENT: >>>",
            f"ARXML: {c['path']}",
            *port_lines(c),
            "<<<",
        ]
        if owner:
            lines += [
                "RELATIONS:",
                "- TYPE: Parent",
                f"  VALUE: {owner}",
            ]
        lines.append("")

        for r in c["runnables"]:
            ruid = uid_for("DES", r["symbol"] or r["path"])
            ev = "; ".join(r["events"]) or "no-event"
            lines += [
                "[REQUIREMENT]",
                f"UID: {ruid}",
                f"TITLE: 可运行实体 {r['name']}",
                "STATEMENT: >>>",
                sdoc_escape(
                    f"{r['name']}（符号 {r['symbol']}，触发：{ev}），"
                    "实现于组件 " + c["name"] + "。"
                ),
                "<<<",
                "COMMENT: >>>",
                f"ARXML: {r['path']}",
                f"SYMBOL: {r['symbol']}",
                f"EVENTS: {ev}",
                "<<<",
                "RELATIONS:",
                "- TYPE: Parent",
                f"  VALUE: {uid}",
                "",
            ]

    for i in model.interfaces:
        uid = uid_for("IF", i["path"])
        lines += [
            "[REQUIREMENT]",
            f"UID: {uid}",
            f"TITLE: 接口 {i['name']}",
            "STATEMENT: >>>",
            sdoc_escape(
                f"{i['name']}（{i['kind']}）："
                f"{', '.join(i['members']) or '(no members)'}"
            ),
            "<<<",
            "COMMENT: >>>",
            f"ARXML: {i['path']}",
            "<<<",
            "",
        ]

    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--arxml", required=True, nargs="+",
                    help="SWC design ARXML file(s) (DaVinci Developer export)")
    ap.add_argument("--name", required=True, help="architecture name (doc title/UID)")
    ap.add_argument("--out-dir", default="docs/davinci")
    ap.add_argument("--fail-on-changes", action="store_true",
                    help="exit 1 when output differs from the previous run (CI gate)")
    args = ap.parse_args()

    model = ArxmlModel(args.arxml[0])
    for extra in args.arxml[1:]:
        model._walk_packages(etree.parse(extra).getroot(), "")

    sdoc = build_sdoc(model, args.name)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{slug(args.name)}.sdoc"
    prev = out_file.read_text(encoding="utf-8") if out_file.exists() else ""
    changed = sdoc != prev
    out_file.write_text(sdoc, encoding="utf-8")

    stats = {
        "compositions": len(model.compositions),
        "components": len(model.components),
        "runnables": sum(len(c["runnables"]) for c in model.components),
        "ports": sum(len(c["ports"]) for c in model.components),
        "interfaces": len(model.interfaces),
        "connectors": sum(len(c["connectors"]) for c in model.compositions),
    }
    report = {
        "name": args.name,
        "arxml": args.arxml,
        "output": str(out_file),
        "content_sha256_16": hashlib.sha256(sdoc.encode()).hexdigest()[:16],
        "changed_vs_previous_run": changed,
        "stats": stats,
        "symbols": [r["symbol"] for c in model.components for r in c["runnables"]],
    }
    (out_dir / "sync_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), "utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if (args.fail_on_changes and changed) else 0


if __name__ == "__main__":
    sys.exit(main())
