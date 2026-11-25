#!/usr/bin/env python3
"""Doxygen XML → StrictDoc 同步工具（SWE.3 详细设计）。

定位：Doxygen 的 `GENERATE_XML` 输出是代码事实的确定性提取器；本工具把
XML 里的函数级文档转成 StrictDoc 的 SWE.3 详细设计条目，**不经过 LLM**。

为什么用 XML 而不是 HTML：XML 是结构化的（符号、参数、位置、类型），
HTML 是渲染结果，解析 HTML 会把版式噪音带进需求库。

映射：

| Doxygen XML                          | StrictDoc                          |
|--------------------------------------|------------------------------------|
| compounddef kind=file                | SECTION（每个源文件一节）           |
| memberdef kind=function              | `DES-<函数名>`，STATEMENT=brief      |
| parameterlist / simplesect(return)   | COMMENT 里的参数表与返回值          |
| detaileddescription（去掉标签）      | COMMENT 里的行为描述                |
| location file:line                   | COMMENT 里的 `Source:` 行（证据）   |
| 注释里的 `@req SWR-xxxx`             | 默认进 COMMENT；`--emit-req-relations` 时生成 `RELATIONS: - TYPE: Parent` |
| compounddef kind=group（defgroup）   | 归属组名进 COMMENT                  |

UID 规则与 sync_davinci_arxml 一致（`DES-<symbol>` slug），这样 Runnable
级（arxml）与函数级（Doxygen）的设计条目在同一套 ID 体系里；同一函数
两边都出现时用 `--uid-prefix` 区分（如 `DESX`），避免 UID 冲突。

用法：

    doxygen Doxyfile        # 需 GENERATE_XML = YES
    python3 sync_doxygen.py --xml-dir build/doxygen/xml \
        --name WindowMgmt --out-dir docs/doxygen
    # CI 门禁：--fail-on-changes
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sync_davinci_arxml import slug, sdoc_escape, uid_for  # noqa: E402


def _strip_tags(el: ET.Element | None) -> str:
    """取元素全部文本并压平空白（Doxygen XML 的 <para> 混排换行）。"""
    if el is None:
        return ""
    text = " ".join(el.itertext()).strip()
    return re.sub(r"\s+", " ", text)


def _clean_description(d: ET.Element | None) -> str:
    """detaileddescription 的行为描述段落：剔除 @req 标签与参数/返回值结构，
    只保留纯叙述段落（结构化信息由 parameterlist / simplesect 单独提取）。"""
    if d is None:
        return ""
    paras: list[str] = []
    for para in d:
        if _local(para.tag) != "para":
            continue
        # 含参数表或返回值的段落跳过
        if para.find("parameterlist") is not None or \
                para.find("simplesect") is not None:
            continue
        txt = re.sub(r"@req\s+\S+", "", " ".join(para.itertext()))
        txt = re.sub(r"\s+", " ", txt).strip()
        if txt:
            paras.append(txt)
    return " ".join(paras)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


class DoxygenModel:
    """从 Doxygen XML 目录构建函数级设计模型。"""

    def __init__(self, xml_dir: str, req_pattern: str) -> None:
        root_dir = Path(xml_dir)
        if not root_dir.is_dir():
            raise FileNotFoundError(f"doxygen xml dir not found: {root_dir}")
        self.functions: list[dict] = []
        self.groups: dict[str, str] = {}  # member name -> group title
        self._group_titles: dict[str, str] = {}
        req_re = re.compile(req_pattern)
        seen: set[str] = set()

        # 第一遍：group compound 收集归属；@defgroup @{ } 块内的函数只出现在
        # group XML（不在 file compound），必须在这里一并收集
        group_compounds: list[ET.Element] = []
        for f in sorted(root_dir.glob("*.xml")):
            if f.name == "index.xml" or f.name == "Doxyfile.xml":
                continue
            cd = ET.parse(f).getroot().find("compounddef")
            if cd is None or cd.get("kind") != "group":
                continue
            group_compounds.append(cd)
            title = _strip_tags(cd.find("title")) or _strip_tags(
                cd.find("compoundname"))
            for m in cd.iter("memberdef"):
                if _local(m.tag) == "memberdef" and m.get("kind") == "function":
                    name = m.findtext("name") or ""
                    if name:
                        self.groups[name] = title
                        self._group_titles[title] = title

        # 第二遍：file compound 收集函数；group 独占的函数补收
        for f in sorted(root_dir.glob("*.xml")):
            if f.name == "index.xml" or f.name == "Doxyfile.xml":
                continue
            cd = ET.parse(f).getroot().find("compounddef")
            if cd is None or cd.get("kind") not in ("file", "struct", "class",
                                                    "namespace"):
                continue
            compound_name = cd.findtext("compoundname") or ""
            for m in cd.iter("memberdef"):
                if m.get("kind") != "function":
                    continue
                name = m.findtext("name") or ""
                if not name or name.startswith("~") or name in seen:
                    continue
                seen.add(name)
                self.functions.append(self._read_function(m, compound_name,
                                                          req_re))

        # group 里独占的函数（@defgroup 块内）
        for cd in group_compounds:
            for m in cd.iter("memberdef"):
                if m.get("kind") != "function":
                    continue
                name = m.findtext("name") or ""
                if not name or name.startswith("~") or name in seen:
                    continue
                seen.add(name)
                self.functions.append(self._read_function(m, "", req_re))

    def _read_function(self, m: ET.Element, compound_name: str,
                       req_re: re.Pattern) -> dict:
        name = m.findtext("name") or ""
        brief = _strip_tags(m.find("briefdescription"))
        detailed = _clean_description(m.find("detaileddescription"))

        params: list[dict] = []
        ret = ""
        d = m.find("detaileddescription")
        if d is not None:
            for pl in d.iter("parameterlist"):
                if pl.get("kind") != "param":
                    continue
                for pi in pl.iter("parameteritem"):
                    names_el = pi.find("parameternamelist")
                    pname = _strip_tags(names_el.find("parametername")) if \
                        names_el is not None else ""
                    direction = ""
                    if names_el is not None:
                        pn = names_el.find("parametername")
                        direction = (pn.get("direction") if pn is not None
                                     else "") or ""
                    params.append({
                        "name": pname,
                        "direction": direction,
                        "description": _strip_tags(pi.find("parameterdescription")),
                    })
            for ss in d.iter("simplesect"):
                if ss.get("kind") == "return":
                    ret = _strip_tags(ss)

        # @req 标签：detaileddescription 全文（含被 ALIASES 展开的文本）
        blob = ""
        if d is not None:
            blob = " ".join(d.itertext())
        reqs = sorted(set(req_re.findall(blob)))

        loc = m.find("location")
        loc_file = loc.get("file", "") if loc is not None else ""
        loc_line = loc.get("line", "") if loc is not None else ""

        definition = _strip_tags_from_string(m.findtext("definition") or "")
        args = _strip_tags_from_string(m.findtext("argsstring") or "")

        return {
            "name": name,
            "signature": f"{definition}{args}".strip(),
            "brief": brief,
            "detailed": detailed,
            "params": params,
            "return": ret,
            "reqs": reqs,
            "group": self.groups.get(name, ""),
            "compound": compound_name,
            "prot": m.get("prot") or "",
            "static": m.get("static") == "yes",
            "loc": f"{loc_file}:{loc_line}" if loc_file else "",
        }


def _strip_tags_from_string(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def build_sdoc(model: DoxygenModel, name: str, uid_prefix: str,
               emit_req_relations: bool) -> str:
    lines = [
        "[DOCUMENT]",
        f"TITLE: Software Detailed Design ({name})",
        f"UID: SDD-{slug(name)}",
        "",
        "[REQUIREMENT]",
        f"UID: SYNC-META-DOX",
        "TITLE: 同步说明",
        "STATEMENT: >>>",
        f"本文件由 sync_doxygen.py 从 Doxygen XML（{name}）自动生成，"
        "单向同步，请勿手工编辑；代码注释修改后重新跑 Doxygen 与本工具。",
        "<<<",
        "",
    ]

    # 按源文件分节
    by_file: dict[str, list[dict]] = {}
    for fn in model.functions:
        key = fn["loc"].rsplit(":", 1)[0] if fn["loc"] else fn["compound"]
        by_file.setdefault(key, []).append(fn)

    for file_key, funcs in sorted(by_file.items()):
        lines += ["[[SECTION]]", f"TITLE: {Path(file_key).name}", ""]
        for fn in funcs:
            uid = uid_for(uid_prefix, fn["name"])
            comment = [
                f"Signature: {fn['signature']}" if fn["signature"] else "",
                f"Source: {fn['loc']}" if fn["loc"] else "",
                f"Group: {fn['group']}" if fn["group"] else "",
                f"Visibility: {'static' if fn['static'] else fn['prot']}",
            ]
            for p in fn["params"]:
                d = f"[{p['direction']}] " if p["direction"] else ""
                comment.append(
                    f"Param {d}{p['name']}: {p['description']}"
                    if p["description"] else f"Param {d}{p['name']}")
            if fn["return"]:
                comment.append(f"Return: {fn['return']}")
            if fn["detailed"]:
                comment.append(f"Behavior: {fn['detailed']}")
            if not emit_req_relations and fn["reqs"]:
                comment.append("Verifies: " + ", ".join(fn["reqs"]))
            comment = [c for c in comment if c]

            lines += [
                "[REQUIREMENT]",
                f"UID: {uid}",
                f"TITLE: {sdoc_escape(fn['brief'] or fn['name'])}",
                "STATEMENT: >>>",
                sdoc_escape(fn["brief"] or "(undocumented)"),
                "<<<",
                "COMMENT: >>>",
                *comment,
                "<<<",
            ]
            if emit_req_relations and fn["reqs"]:
                lines.append("RELATIONS:")
                for req in fn["reqs"]:
                    lines += ["- TYPE: Parent", f"  VALUE: {req}"]
            lines.append("")
        lines += ["[[/SECTION]]", ""]

    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--xml-dir", required=True, help="Doxygen XML 输出目录")
    ap.add_argument("--name", required=True, help="文档名")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--uid-prefix", default="DES",
                    help="设计条目 UID 前缀（与 arxml 同步冲突时改，如 DESX）")
    ap.add_argument("--req-pattern",
                    default=r"(?:@req|Requirement:)\s*([A-Z]{2,6}-\d{2,6})",
                    help="@req 标签匹配正则（默认兼容 @req 与 ALIASES 展开形式）")
    ap.add_argument("--emit-req-relations", action="store_true",
                    help="把 @req 生成 RELATIONS: Parent（要求需求文档同仓）")
    ap.add_argument("--fail-on-changes", action="store_true",
                    help="输出与上次不同则退出码 1（CI 门禁）")
    args = ap.parse_args()

    report: dict = {"name": args.name}
    try:
        model = DoxygenModel(args.xml_dir, args.req_pattern)
    except FileNotFoundError as exc:
        report.update({"ok": False, "error": str(exc)})
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 2

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    doc_name = args.name.lower()
    sdoc_path = out_dir / f"{doc_name}.sdoc"
    sdoc_text = build_sdoc(model, args.name, args.uid_prefix,
                           args.emit_req_relations)

    previous = sdoc_path.read_text(encoding="utf-8") if sdoc_path.is_file() \
        else None
    changed = previous != sdoc_text
    sdoc_path.write_text(sdoc_text, encoding="utf-8")

    report.update({
        "ok": True,
        "output": str(sdoc_path),
        "changed_vs_previous_run": changed,
        "stats": {
            "functions": len(model.functions),
            "files": len({fn["loc"].rsplit(":", 1)[0] for fn in model.functions
                          if fn["loc"]}),
            "req_tags": sum(len(fn["reqs"]) for fn in model.functions),
        },
        "functions": [fn["name"] for fn in model.functions],
        "req_map": {fn["name"]: fn["reqs"] for fn in model.functions
                    if fn["reqs"]},
    })
    (out_dir / "sync_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    print(json.dumps(report, ensure_ascii=False, indent=1))
    if args.fail_on_changes and changed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
