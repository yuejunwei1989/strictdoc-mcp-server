"""Smoke test: call the index layer directly (no MCP handshake needed).

Run from the repo root after exporting the example project:
    strictdoc export example/docs --formats=json --output-dir example/output
    python tests/smoke.py
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "example" / "output" / "json" / "index.json"

if not OUT.is_file():
    subprocess.run(
        ["strictdoc", "export", "example/docs", "--formats=json",
         "--output-dir", "example/output"],
        cwd=ROOT, check=True, capture_output=True,
    )

sys.path.insert(0, str(ROOT / "src"))
from strictdoc_mcp.index import StrictDocIndex  # noqa: E402

idx = StrictDocIndex(OUT)
ok = True


def check(name: str, cond: bool, detail: str = "") -> None:
    global ok
    print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if detail else ""))
    ok = ok and cond


s = idx.summary()
check("summary", s["documents"] == 3 and s["requirements"] == 7 and s["sections"] == 2,
      json.dumps(s, ensure_ascii=False))

docs = idx.list_documents()
check("documents", {d["uid"] for d in docs} == {"SYRS", "SWRS", "SADS"})

req = idx.get_requirement("SWR-0002")
check("get_requirement fields", req["fields"]["STATEMENT"].strip().startswith("软件应"))
check("breadcrumb", req["section_path"] == ["车窗控制"], str(req["section_path"]))
declared = req["relations"]["declared"]
check("declared resolved", len(declared) == 1 and declared[0]["uid"] == "SYS-0001" and declared[0]["resolved"])
refby = req["relations"]["referenced_by"]
check("referenced_by ARCH-0012", any(i["uid"] == "ARCH-0012" for i in refby), str(refby))

tr = idx.traceability("DES-0101")
up_last = " | ".join(c["chain"][-1] for c in tr["upstream"])
check("trace upstream reaches SYS-0001 and SYS-0002",
      "SYS-0001" in up_last and "SYS-0002" in up_last, up_last)
check("trace source refs", all(c["source"] for c in tr["upstream"]))
tr2 = idx.traceability("SYS-0001")
down_last = " ".join(c["chain"][-1] for c in tr2["downstream"])
check("trace downstream from SYS-0001 reaches DES-0101", "DES-0101" in down_last, down_last)

hits = idx.search("防夹")
check("search CJK", any(h["uid"] == "SWR-0002" for h in hits), str(len(hits)))
hits2 = idx.search(status="Draft")
check("search by status", {h["uid"] for h in hits2} == {"SWR-0002", "DES-0101"})

orphans = idx.find_orphans()
check("orphans no_upstream", {r["uid"] for r in orphans["no_upstream"]} == {"SYS-0001", "SYS-0002"})
check("orphans no_downstream", {r["uid"] for r in orphans["no_downstream"]} == {"DES-0101"},
      str(orphans["no_downstream"]))

check("dangling detection", s["unresolved_relations"] == 0)

sys.exit(0 if ok else 1)
