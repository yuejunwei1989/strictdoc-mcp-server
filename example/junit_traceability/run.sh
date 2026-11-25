#!/usr/bin/env bash
# 需求 ↔ 测试用例 ↔ 测试结果 全链路演示
set -euo pipefail
cd "$(dirname "$0")"

# 1. 跑测试并产出 StrictDoc 可识别的 JUnit 文件名（*.pytest.junit.xml）
pytest tests --junitxml=reports/tests.pytest.junit.xml

# 2. 导出（必须显式 --config）
strictdoc export . --config project_config.py --formats=html --output-dir out

echo "打开 out/html/index.html 查看需求页的 RELATIONS (File) 与 out/html/source_coverage.html"
