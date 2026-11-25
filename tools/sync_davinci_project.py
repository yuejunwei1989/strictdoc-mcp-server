#!/usr/bin/env python3
"""DaVinci Developer 工程级架构同步编排器。

定位：sync_davinci_arxml.py 只吃 arxml 文件；真实项目里架构往往分散在
.dcf 工作区 / .dpa 工程引用的多个输入文件里。本脚本负责三件事：

1. **输入解析**：从 .dcf / .dpa 工程文件中解析出 arxml 输入文件集合
   （工程文件本身是 XML/文本，确定性解析，不需要 DaVinci）。
2. **DaVinci 校验（可选）**：如果本机有 dvdevc（DaVinci Developer 4.17 SP2+），
   先跑 `project convert --analysis-only`（只读校验，不修改工程），
   确认工程能被 DaVinci 干净加载——这是直接解析 arxml 拿不到的准确性增益。
3. **复用既有同步**：把解析出的 arxml 集合交给 sync_davinci_arxml 的
   ArxmlModel / build_sdoc，产出与单文件模式完全一致的 SDoc + 报告。

与 davinci-mcp-server 的关系：dvdevc 的发现逻辑沿用同一套约定
（DAVINCI_DVDEVC_BIN 环境变量 / PATH 自动发现），校验步骤等价于
davinci_dev_workspace_convert(analysis_only=True, dry_run 语义)。

用法：
    # 从 .dcf 工作区解析输入并校验
    python3 sync_davinci_project.py --project MyProject.dcf --name MyProject

    # 无 dvdevc 环境也可用（跳过校验，仅解析+同步）
    python3 sync_davinci_project.py --project MyProject.dcf --skip-validation

    # 直接给 arxml 清单（绕过工程解析）
    python3 sync_davinci_project.py --arxml a.arxml b.arxml --name X

    # CI 门禁：内容与上次不同则退出码 1
    python3 sync_davinci_project.py --project P.dcf --fail-on-changes
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sync_davinci_arxml import ArxmlModel, build_sdoc  # noqa: E402

# --------------------------------------------------------------------- dvdevc


def find_dvdevc(explicit: str | None) -> Path | None:
    """发现 dvdevc：显式参数 > DAVINCI_DVDEVC_BIN > PATH。"""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env_bin = os.environ.get("DAVINCI_DVDEVC_BIN")
    if env_bin:
        candidates.append(Path(env_bin))
    for c in candidates:
        if c.is_file():
            return c
    for name in ("dvdevc.exe", "dvdevc"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def dvdevc_analyze(dvdevc: Path, workspace: Path, timeout_s: int) -> dict:
    """只读校验：dvdevc project convert --analysis-only。

    该命令让 DaVinci Developer 自己加载工程并报告问题，不修改任何文件。
    退出码：0 成功；1 警告；2 用法错误；3 ACCNOK；7 WSNOK。
    """
    cmd = [
        str(dvdevc), "project", "convert",
        f"--workspace-path={workspace}",
        "--analysis-only",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout_s, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        return {"ok": False, "reason": f"dvdevc not executable: {dvdevc}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": f"timeout after {timeout_s}s", "command": cmd}

    tail = (proc.stdout or "").strip().splitlines()[-8:]
    ok = proc.returncode in (0, 1)  # 1 = 有警告但可加载
    return {
        "ok": ok,
        "returncode": proc.returncode,
        "warnings": proc.returncode == 1,
        "command": cmd,
        "stdout_tail": tail,
        "stderr_tail": (proc.stderr or "").strip().splitlines()[-4:],
    }


# ------------------------------------------------------------ project parsing

# 匹配工程文件里出现的 arxml 引用：XML 属性值、文本节点、Windows/POSIX 路径。
# DaVinci 的 .dpa / .dcf 是 XML，路径通常出现在 value/ref/uri 类属性里；
# 用宽松正则再逐个验证存在性，避免依赖私有的 schema 细节。
# 注意：不处理带空格的路径（DaVinci uri 通常不含空格）。
_ARXML_REF = re.compile(r"[\w.\-\\/]+\.arxml", re.IGNORECASE)


def resolve_arxml_inputs(project_file: Path) -> dict:
    """从 .dcf/.dpa 工程文件解析 arxml 输入集合。

    返回 resolved（存在且去重的绝对路径列表）与 missing（引用了但找不到）。
    """
    base = project_file.resolve().parent
    text = project_file.read_text(encoding="utf-8", errors="replace")
    seen: set[Path] = set()
    resolved: list[Path] = []
    missing: list[str] = []
    for m in _ARXML_REF.finditer(text):
        raw = m.group(0).replace("\\", "/")
        # 排除生成目录 / Output / GenData 这类 DaVinci 产物
        lowered = raw.lower()
        if any(seg in lowered for seg in ("/output/", "/gendata/", "\\output\\", "\\gendata\\")):
            continue
        p = Path(raw)
        if not p.is_absolute():
            p = (base / p)
        p = p.resolve()
        if p in seen:
            continue
        seen.add(p)
        if p.is_file():
            resolved.append(p)
        else:
            missing.append(raw)
    return {"resolved": resolved, "missing": missing}


# ----------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--project", help=".dcf 工作区或 .dpa 工程文件")
    src.add_argument("--arxml", nargs="+", help="直接指定 arxml 输入（跳过工程解析）")
    ap.add_argument("--name", required=True, help="文档名（决定输出文件名与文档标题）")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--dvdevc", help="dvdevc 可执行文件路径（缺省自动发现）")
    ap.add_argument("--skip-validation", action="store_true",
                    help="跳过 dvdevc --analysis-only 校验")
    ap.add_argument("--dvdevc-timeout", type=int, default=900)
    ap.add_argument("--ignore-validation", action="store_true",
                    help="dvdevc 校验失败仍继续（警告会保留在报告中）")
    ap.add_argument("--fail-on-changes", action="store_true",
                    help="输出与上次运行不同时退出码 1（CI 门禁）")
    args = ap.parse_args()

    report: dict = {"name": args.name, "source": args.project or "direct arxml"}

    # 1. 解析输入集合
    if args.project:
        pf = Path(args.project)
        if not pf.is_file():
            print(json.dumps({"ok": False, "error": f"project not found: {pf}"}))
            return 2
        inputs = resolve_arxml_inputs(pf)
        report["resolved_inputs"] = [str(p) for p in inputs["resolved"]]
        report["missing_inputs"] = inputs["missing"]
        if not inputs["resolved"]:
            report["ok"] = False
            report["error"] = "工程文件里没有解析到任何存在的 .arxml 引用"
            report["hint"] = ("DaVinci 工程格式有版本差异；如解析失败，"
                              "用 --arxml 手动指定输入文件")
            print(json.dumps(report, ensure_ascii=False, indent=1))
            return 2
        arxml_files = inputs["resolved"]
    else:
        arxml_files = [Path(a).resolve() for a in args.arxml]
        bad = [str(p) for p in arxml_files if not p.is_file()]
        if bad:
            report["ok"] = False
            report["error"] = f"arxml not found: {bad}"
            print(json.dumps(report, ensure_ascii=False, indent=1))
            return 2
        report["resolved_inputs"] = [str(p) for p in arxml_files]

    # 2. dvdevc 只读校验
    if args.skip_validation:
        report["validation"] = {"skipped": True}
    else:
        workspace: Path | None = None
        if args.project and Path(args.project).suffix.lower() == ".dcf":
            workspace = Path(args.project).resolve()
        dvdevc = find_dvdevc(args.dvdevc)
        if dvdevc is None:
            report["validation"] = {
                "skipped": True,
                "reason": ("dvdevc 未找到（需要 DaVinci Developer 4.17 SP2+）；"
                           "继续同步但未经 DaVinci 校验"),
            }
        elif workspace is None:
            report["validation"] = {
                "skipped": True,
                "reason": ("dvdevc 校验需要 .dcf 工作区（project convert "
                           "--analysis-only 基于 workspace）；本次输入不是 .dcf"),
            }
        else:
            validation = dvdevc_analyze(dvdevc, workspace, args.dvdevc_timeout)
            report["validation"] = validation
            if not validation["ok"] and not args.ignore_validation:
                report["ok"] = False
                report["error"] = "dvdevc 校验失败，中止同步（--ignore-validation 可跳过）"
                print(json.dumps(report, ensure_ascii=False, indent=1))
                return 3

    # 3. 复用 sync_davinci_arxml 构建模型与 SDoc
    model = ArxmlModel(str(arxml_files[0]))
    for extra in arxml_files[1:]:
        import lxml.etree as etree
        model._walk_packages(etree.parse(str(extra)).getroot(), "")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    doc_name = args.name.lower()
    sdoc_path = out_dir / f"{doc_name}.sdoc"
    sdoc_text = build_sdoc(model, args.name)

    previous = sdoc_path.read_text(encoding="utf-8") if sdoc_path.is_file() else None
    changed = previous != sdoc_text
    sdoc_path.write_text(sdoc_text, encoding="utf-8")

    report.update({
        "ok": True,
        "output": str(sdoc_path),
        "changed_vs_previous_run": changed,
        "stats": {
            "compositions": len(model.compositions),
            "components": len(model.components),
            "interfaces": len(model.interfaces),
            "arxml_files": len(arxml_files),
        },
        "symbols": sorted(
            r["symbol"] for c in model.components for r in c["runnables"] if r["symbol"]
        ),
    })
    rep_path = out_dir / "sync_report.json"
    rep_path.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                        encoding="utf-8")

    print(json.dumps(report, ensure_ascii=False, indent=1))
    if args.fail_on_changes and changed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
