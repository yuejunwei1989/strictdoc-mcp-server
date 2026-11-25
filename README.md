# strictdoc-mcp

**只读**的 StrictDoc MCP server：把 StrictDoc 的需求图（需求、追溯链、缺口）以 MCP 工具的形式暴露给 AI Agent（Claude Code / Cursor / Copilot 等）。

设计原则（对应 ASPICE 语境下的治理要求）：

- **只读**：不提供任何写工具。Agent 不能改基线，所有修改走 `.sdoc` 文件的 PR 评审。
- **强制溯源**：每个工具返回都带 `source` 字段（`<文档UID>.sdoc#UID=<条目UID>`），Agent 引用需求时必须保留出处。
- **Agent 与 CI 同源**：Agent 读的 JSON 和 CI 检查用的都是同一条 `strictdoc export --formats=json` 产物，不会漂移。
- **自动重载**：JSON 文件 mtime 变化时自动重建索引，重新导出即可刷新。

## 工作原理

```
docs/*.sdoc ──strictdoc export --formats=json──> output/json/index.json
                                                        │
                                              strictdoc-mcp（只读解析）
                                                        │
                                        MCP 工具：get_requirement / get_traceability / ...
                                                        │
                                              Claude Code / Cursor 等 Agent
```

## 安装

```bash
# 先生成 JSON 导出（在你的 StrictDoc 项目里）
pip install strictdoc        # >= 0.30
strictdoc export docs --formats=json --output-dir output

# 安装本 server
pip install -e /path/to/strictdoc-mcp
```

依赖 `mcp>=1.6`（同时兼容 mcp 1.x 的 `FastMCP` 与 2.x 的 `MCPServer`）。

## 运行

三种方式指定 JSON 导出路径（按优先级）：

```bash
strictdoc-mcp /path/to/output/json/index.json   # 1. 第一个位置参数
STRICTDOC_MCP_JSON=/path/to/index.json strictdoc-mcp   # 2. 环境变量
cd <含 output/ 的项目根> && strictdoc-mcp        # 3. 默认找 ./output/json/index.json 或 ./json/index.json
```

## MCP 客户端配置

Claude Code（项目根 `.mcp.json`）：

```json
{
  "mcpServers": {
    "strictdoc": {
      "command": "strictdoc-mcp",
      "args": ["/abs/path/to/output/json/index.json"]
    }
  }
}
```

Cursor / Claude Desktop（`mcp.json` / `claude_desktop_config.json`）：

```json
{
  "mcpServers": {
    "strictdoc": {
      "command": "strictdoc-mcp",
      "args": ["/abs/path/to/output/json/index.json"],
      "env": { "STRICTDOC_MCP_JSON": "/abs/path/to/output/json/index.json" }
    }
  }
}
```

## 工具列表（全部只读）

| 工具 | 用途 |
|---|---|
| `strictdoc_info` | 数据集总览：文档数、需求按状态分布、关系数、**悬空关系数** |
| `list_documents` | 文档清单及各自的需求 UID 列表 |
| `get_document_tree(doc_uid)` | 单个文档的章节/需求树 |
| `get_requirement(uid)` | 单条需求的全部字段、章节路径、`declared`（本节点声明的关系，通常 Parent→上游）与 `referenced_by`（谁引用了我），带解析后的目标状态与出处 |
| `get_traceability(uid, max_depth)` | 完整上下游追溯链，每一跳带 `source` |
| `search_requirements(query, status, doc_uid, limit)` | 按 UID/标题/正文子串搜索，可按状态、文档过滤 |
| `find_orphans` | 缺口分析：`no_upstream`（没声明任何上游）与 `no_downstream`（没有任何下游条目指向它，即未被实现/验证引用） |

返回全部为 JSON 字符串（`ensure_ascii=False`，中文原文保留）。

## 典型 Agent 工作流

在 Skill / AGENTS.md 里给 Agent 立规矩（要点）：

```markdown
## 输入契约（禁止自行 grep 需求文件）
1. MCP: get_requirement(id)      → 需求原文 + 字段 + 出处
2. MCP: get_traceability(id)     → 上下游影响范围
3. MCP: find_orphans()           → 评估前缺口自检

## 生成规则
- 每条陈述必须带 [source: <工具返回的 source 字段>]
- 工具返回 UNRESOLVED 的关系一律标注 [DANGLING]，不得脑补目标条目
- 禁止：修改任何 .sdoc 文件、发明 UID、推断需求间关系
```

评估前跑一遍 `find_orphans` + `strictdoc_info` 里的 `unresolved_relations`，能提前暴露"孤儿需求"和"悬空引用"两类最常见 finding。

## 从 Polarion 同步需求（单向，确定性脚本）

`tools/sync_polarion.py` 把 Polarion 工作项同步为 StrictDoc 文档。**不经过 LLM**——批量搬运需求要的是可复现和幂等，不是智能：

```
Polarion REST (2023 兼容) ──sync_polarion.py──> docs/polarion/<PROJECT>.sdoc + sync_report.json
                                                        │
                                        strictdoc export --formats=json
                                                        │
                                              strictdoc-mcp（Agent 只读查询）
```

```bash
export POLARION_URL=https://polarion.example.com
export POLARION_USERNAME=alice POLARION_PASSWORD=secret   # 或 POLARION_TOKEN
python tools/sync_polarion.py --project DEMO \
    --query "type:requirement" \
    --out-dir docs/polarion \
    --status-map open=draft,closed=approved \
    --parent-roles parent,derives
```

| 映射 | Polarion 侧 | StrictDoc 侧 |
|---|---|---|
| 条目标识 | work item id（如 `DEMO-1`） | UID 原样保留，跨系统核对不漂移 |
| 标题 | `title` | TITLE（压成单行） |
| 正文 | `description`（HTML） | STATEMENT（去标签转纯文本） |
| 状态 | `status` 枚举 | STATUS，可用 `--status-map` 重映射 |
| 层级 | role ∈ `--parent-roles` 的出链 | `RELATIONS: - TYPE: Parent` |
| 其他 | 自定义字段、非 parent 链接、修订时间 | COMMENT（含 `Polarion: <id>` 溯源行） |

要点：

- **单向**：Polarion 是权威源，生成文件头有同步说明，禁止手改 `.sdoc`；改动回 Polarion 再同步。
- **幂等**：同参数重跑内容逐字节一致；`sync_report.json` 记录条目清单与内容哈希。
- **CI 门禁**：加 `--fail-on-changes`，Polarion 与仓库漂移时构建变红，这就是 ASPICE 变更管理的证据。
- **与本项目 MCP server 的关系**：同步脚本负责"搬运"（确定性），strictdoc-mcp 负责"让 Agent 读"（查询层）。两者消费同一份 JSON 导出，不会漂移。
- 已在 `pvliesdonk` 风格的 fake Polarion 2310（[polarion-mcp-2023](https://github.com/yuejunwei1989/polarion-mcp-2023) 自带的测试服务器）上端到端验证：工作项+自定义字段+parent 链接 → `.sdoc` → JSON 导出 → MCP 查询，全链路通过。

## 从 DaVinci（AUTOSAR SWC ARXML）同步架构（单向，确定性脚本）

`tools/sync_davinci_arxml.py` 把 DaVinci Developer 的 SWC 设计 ARXML 同步为 SWE.2 架构文档。**不调 DaVinci CLI，也不经过 LLM**——arxml 本身就是架构权威源，直接只读解析；DaVinci CLI（校验/生成代码）归 davinci-mcp-server 管，两者解耦：

```
DaVinci Developer (.arxml 设计文件)
        │ sync_davinci_arxml.py（lxml 只读解析，无需 DaVinci 安装）
        ▼
docs/davinci/<name>.sdoc  +  sync_report.json（含 SYMBOL 清单）
        │ strictdoc export --formats=json
        ▼
strictdoc-mcp（Agent 查询架构追溯图）
```

```bash
python tools/sync_davinci_arxml.py --arxml WindowMgmt.swc.arxml \
    --name WindowMgmt --out-dir docs/davinci
# CI 门禁：--fail-on-changes
```

| ARXML 元素 | StrictDoc | 说明 |
|---|---|---|
| `COMPOSITION-SW-COMPONENT-TYPE` | `ARCH-<包路径>` | 连接器与组件原型进 COMMENT |
| `SW-COMPONENT-TYPE` | `ARCH-<包路径>`，Parent→所属组合 | 端口表（含接口成员）进 COMMENT |
| `RUNNABLE-ENTITY` | `DES-<SYMBOL>`，Parent→所属组件 | **SYMBOL 是代码锚点**，可与 Doxygen 符号/`@req` 标签比对 |
| `TIMING-EVENT` 等 | Runnable 的 COMMENT | 触发方式（cyclic 周期/事件） |
| `PORT-INTERFACE`（含 DaVinci 的包装式导出） | `IF-<包路径>` | 接口成员列表 |

要点：

- **UID 用逻辑 SHORT-NAME 路径**（`/WindowMgmt/AntiPinch` → `ARCH-windowmgmt-antipinch`），不用物理文件路径——文件重命名不改变 UID，避免每次导入全量新建。
- **幂等**：同输入重跑逐字节一致；`sync_report.json` 记录统计与 SYMBOL 清单。
- **命名空间无关解析**：兼容 R4.0+ 默认命名空间与 DaVinci 的 `PORT-INTERFACE` 包装导出。
- 已知限制：仅覆盖组件/接口/行为层；`DELEGATION-SW-CONNECTOR`、变体、多包交叉引用（需 SHORT-NAME-PATH 解引用表）尚未实现。

### 工程级入口：sync_davinci_project.py

真实项目里架构分散在 `.dcf` 工作区 / `.dpa` 工程引用的多个 arxml 里。`tools/sync_davinci_project.py` 做三步编排：

```
.dcf/.dpa 工程 ──解析引用──▶ arxml 输入集合 ──dvdevc --analysis-only 校验──▶ sync_davinci_arxml
```

1. **输入解析**：从工程文件提取所有 `.arxml` 引用（排除 Output/GenData），相对路径按工程目录解析，缺失引用进 `missing_inputs`。
2. **DaVinci 校验（可选）**：若本机有 `dvdevc`（DaVinci Developer 4.17 SP2+，发现逻辑与 davinci-mcp-server 一致：`--dvdevc` 参数 > `DAVINCI_DVDEVC_BIN` > PATH），先跑 `project convert --analysis-only`（**只读**，不修改工程）确认工程能被 DaVinci 干净加载；失败即中止同步（`--ignore-validation` 跳过）。
3. **同步**：多个 arxml 合并进一个模型，产出与单文件模式一致的 SDoc + 报告。

```bash
# 从 .dcf 工作区同步（有 DaVinci 的机器上）
python tools/sync_davinci_project.py --project MyProject.dcf --name MyProject --out-dir docs/davinci

# 无 DaVinci 环境（跳过校验，仅解析+同步）
python tools/sync_davinci_project.py --project MyProject.dcf --skip-validation ...

# CI 门禁
python tools/sync_davinci_project.py --project MyProject.dcf --fail-on-changes ...
```

说明：
- 校验步骤等价于 davinci-mcp-server 的 `davinci_dev_workspace_convert(analysis_only=True)`；退出码 3（ACCNOK）/7（WSNOK）语义一致。
- `.dpa` 工程只做输入解析（dvdevc 的 analysis-only 校验基于 `.dcf`）；如解析不到引用（DaVinci 版本格式差异），用 `--arxml` 手动指定。
- 工程文件解析是启发式正则（不依赖 DaVinci 私有 schema），不处理含空格的路径。

## 从 Doxygen 同步详细设计（SWE.3，单向，确定性脚本）

`tools/sync_doxygen.py` 把 Doxygen 的 XML 输出转成 SWE.3 详细设计条目。**用 XML 不用 HTML**：XML 是结构化事实（符号、参数、位置），HTML 是渲染结果。**不经过 LLM**。

```
代码注释（@brief/@param/@req）
        │ doxygen Doxyfile（需 GENERATE_XML = YES）
build/doxygen/xml/*.xml
        │ sync_doxygen.py（确定性解析）
docs/doxygen/*.sdoc ──▶ 与需求文档合并导出 / ReqIF
```

```bash
python tools/sync_doxygen.py --xml-dir build/doxygen/xml \
    --name WindowMgmt --out-dir docs/doxygen
# CI 门禁：--fail-on-changes
```

| Doxygen XML | StrictDoc | 说明 |
|---|---|---|
| compounddef kind=file | SECTION（每源文件一节） | |
| memberdef kind=function | `DES-<函数名>`，STATEMENT=brief | UID 与 arxml 同步同一体系 |
| parameterlist / simplesect(return) | COMMENT 参数表与返回值 | 含 direction（in/out） |
| detaileddescription（纯叙述段落） | COMMENT `Behavior:` 行 | 已剔除参数/返回值混排 |
| location file:line | COMMENT `Source:` 行 | **评审证据：每条陈述可回溯到代码行** |
| 注释里 `@req SWR-xxxx` | 默认 COMMENT `Verifies:`；`--emit-req-relations` 时生成 `RELATIONS: Parent` | 关系模式要求需求文档同仓（断链会被 StrictDoc 卡） |
| `@defgroup` 块内函数 | 归属组名进 COMMENT | 只出现在 group XML，不漏收 |

要点：

- `--req-pattern` 默认同时兼容 `@req SWR-0002` 与 Doxygen ALIASES 展开形式（`Requirement: SWR-0002`）。
- **UID 冲突提示**：arxml 同步（Runnable 级）与 Doxygen 同步（函数级）都用 `DES-<名>`；同一函数两边出现时给 Doxygen 加 `--uid-prefix DESX` 区分。
- 已知限制：只覆盖函数级（class/struct 的成员函数同样处理）；调用图、算法流程图不在 Doxygen XML 里，设计意图仍需人工/AI 起草补充。

## 测试框架集成：需求 ↔ 用例 ↔ 测试结果

StrictDoc 0.30.4 原生支持读取测试报告（官方仍标注 experimental，但功能可用）。**不需要写转换器**，只要报告文件名匹配它认识的 flavor：

| 框架 | 报告文件名要求 | 产出方式 |
|---|---|---|
| **pytest** | `*.pytest.junit.xml` | `pytest --junitxml=reports/tests.pytest.junit.xml` |
| **CTest** | `*.ctest.junit.xml` | `ctest --output-junit reports/tests.ctest.junit.xml` |
| **Google Test** | `*.gtest.junit.xml` | 经 ctest JUnit 输出（测试函数名匹配含 `#GTEST#` 特判） |
| **LLVM LIT** | `*.lit.junit.xml` | 需在 config 里用 `test_report_root_dict` 指定测试根目录 |
| **cargo-nextest** | `*.nextest.junit.xml` | `.config/nextest.toml` 开 JUnit reporter，按 Rust canonical path 匹配 |
| **Robot Framework** | `*.robot.xml` | 原生 `output.xml`，非 JUnit 格式 |
| Unity/Ceedling、VectorCAST、Tessy、Cantata 等 | 若产出 JUnit XML，需与上述 flavor 的字段约定一致 | 不一致时用脚本转 SDoc（见 `tools/`） |

### 关联机制（关键，不是靠文件名猜）

```
测试源码函数里的 @relation(SWR-0001, scope=function)
        ↓  StrictDoc 源码解析（tree-sitter：Python / C / C++ / Rust / Robot）
测试函数 ←匹配← JUnit testcase（classname + name 定位源码函数）
        ↓
TEST_RESULT 节点（STATUS: PASSED/FAILED）挂到测试函数 → 测试函数挂到需求
        ↓
需求页显示 RELATIONS (File)，source_coverage 统计函数覆盖率
```

C 代码示例：

```c
/**
 * @brief 防夹反向响应时间验证
 * @relation(SWR-0001, scope=function)
 */
TEST(AntiPinch, ReverseWithin100ms) { ... }
```

### 最小可跑示例

`example/junit_traceability/`（`./run.sh` 一键跑通）：

```bash
pytest tests --junitxml=reports/tests.pytest.junit.xml
strictdoc export . --config project_config.py --formats=html --output-dir out
```

验证结果：需求页出现 `RELATIONS (File): tests/test_anti_pinch.py, lines: 7-11, function test_reverse_within_100ms()`；`source_coverage.html` 显示函数覆盖率 100%（2/2）；失败的用例在 TEST_RESULT 节点里 `STATUS: FAILED`，如实反映。

### 四个实测踩到的坑

1. **`project_config.py` 不会被自动加载**，必须 `strictdoc export . --config project_config.py`。不加时一切静默降级，看不出错。
2. **`project_features` 必须传 `ProjectFeature` 枚举**（`ProjectFeature.REQUIREMENT_TO_SOURCE_TRACEABILITY`）。传裸字符串会被静默丢弃——`get_features()` 返回空列表，追溯完全不生效。
3. **报告文件名后缀必须是 StrictDoc 认识的 flavor**（`tests.pytest.junit.xml` 而不是 `junit.xml`），否则文件被当普通文档忽略。
4. **pytest 的 rootdir 必须与 StrictDoc 工程根对齐**。pytest 会向上找 `pyproject.toml`，若停在仓库根，classname 会变成 `example.junit_traceability.tests...`，StrictDoc 解析出的 `TEST_PATH` 带目录前缀，报 `references a file that does not exist`。示例里用本地 `pytest.ini` 固定 rootdir。

> 与 strictdoc-mcp 的配合：TEST_RESULT 节点有 UID（如 `tests.test_anti_pinch.test_reverse_within_100ms`），可用 `get_requirement` 按 UID 查 `STATUS` 和 `TEST_FUNCTION`；但 `search_requirements` 目前只检索 REQUIREMENT 类型，测试结果要靠 UID 或 `find_orphans` 之外的脚本查。

## 已知限制

- JSON 导出不含 `.sdoc` 文件路径与行号，`source` 以"文档 UID + 条目 UID"定位（在 Git 仓库里可唯一映射回文件）。
- 索引随 JSON mtime 自动重载；改了 `.sdoc` 记得重新 `strictdoc export`。
- 只处理 UID 唯一的项目（StrictDoc 本身要求 UID 唯一，重复 UID 以最后一个为准并在 `strictdoc_info` 中可观察异常计数）。

## 测试

```bash
strictdoc export example/docs --formats=json --output-dir example/output
python tests/smoke.py    # 13 项断言，含中文检索、双向追溯、孤儿/悬空检测
```

## 许可

MIT
