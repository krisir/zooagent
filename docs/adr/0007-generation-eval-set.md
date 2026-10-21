# ADR-0007 生成质量 Eval 集：固定需求 + 结构化期望

- 状态：确定
- 日期：2026-10-20

## 背景

设计文档 **13.4 AI 生成质量测试**要求把"AI 生成的应用是否正确"变成可回归的测试，而不是靠人工感觉。触发这一决策的两个具体问题：

1. **换模型 / 改 prompt 没有回归保护**。主助手的行为由模型驱动，改一处提示词或换一个 LLM 供应商，很可能悄悄破坏某个领域的生成质量（少建金额字段、关系指向不存在的模型、页面没有录入入口），但现有测试只覆盖离线规划器与固定契约。
2. **相同需求在不同模型下产物不同**，需要一种**横向比较**手段，且比较的是"应用能不能干活"，而不是字面文案。

## 决策

建立一套**数据驱动**的评测集，位置 `backend/eval/`：

- **用例是数据，不是代码**。`eval/cases.toml` 用 TOML（标准库 `tomllib`，无新依赖）描述：`prompt`/`turns`、必须通过的 `providers`、结构化 `expect`，以及多轮用例的 `evolution` 不变量。
- **打分对象是引擎真正建出来的应用**。快照（`eval/snapshot.py:capture_snapshot`）直接从 Model / Data / UI 引擎读取，因此评测的是平台真实产物，而不是模型的自述。
- **期望只检查结构可用性**，不比对文案：
  - 计数区间：模型数、字段数、关系数、Action 数、页面数、记录数、工具调用与失败数、中文字段名比例。
  - 语义字段 `concept_fields`：用 `type + hints`（hint 匹配字段 key / 中文名）而不是精确 key，因为 key 由 AI 决定。这样对模型选择保持宽容，但能发现"根本没建金额字段"这类错误。
  - 完整性布尔项：`relation_targets_resolve`、`actions_valid`、`core_ops_covered`（每个模型都有页面引用**且**存在可录入的表格/表单）、`no_default_on_required`、`enum_options_present`、`keys_valid`。
  - 安全项：`forbidden_key_fragments` + `no_sql_like_keys`，用于提示注入用例。
- **每个检查都返回可读原因**（例如「这些模型没有任何页面引用: ['loan']」），失败时直接告诉人**哪里不对**。
- **演化用例对首末两轮快照做差分**：`field_superset`（只能加字段）、`records_preserved`（旧记录 id 与内容摘要 sha1 必须仍在）、`version_increased`、`new_concept_fields`。这条对应设计文档第 4/5 条"追加需求不能丢数据"。

### 用例覆盖

覆盖设计文档点名的五个领域（**家庭记账、客户管理、简单库存、个人物品管理、学习计划与进度**），外加两个质量用例：**提示注入**（安全性）与**模糊需求**（不应凭空造大结构）。

### provider 分层与 CI

用例声明它要在哪些 provider 类型上通过：

| provider | 含义 | 需要密钥 |
| --- | --- | --- |
| `offline` | 内置模板规划器 | 否 |
| `mock` | AgentScope 接口的 mock 模型 | 否 |
| `llm` | 任一已配置的真实模型 | 是 |

`backend/tests/agent_eval/test_eval_cases.py` 把**无密钥子集**接进 `uv run pytest`（并断言五个领域都在用例文件里）；`test_checks.py` 用合成快照单测**检查器本身**（"少建金额字段必须失败"、"模型丢字段必须被 evolution 抓到"）。需要密钥的用例（演化、模糊需求等）在 pytest 里被**跳过而非失败**，用 `uv run python -m zooagent.eval --provider llm` 手动跑。

## 后果

- 换模型、改 prompt、调工具时有了回归网：跑一遍 `zooagent.eval` 即可看到结构指标与状态变化，`--json` 存结果 + `--compare` 看差异。
- 无密钥子集进 CI，不消耗任何 token；真实模型用例按需手动跑（`--workspace` 复用用户已配置的 profile，包括 API Key）。
- **离线/mock 规划器无法做"演化"**：它们按模板从零创建应用，不具备增量理解能力（模型版本化 + 按 key 增量合并本身是支持的）。因此演化用例目前只跑 `llm`——这是用例分层，不是平台缺陷。
- 用例刻意保持"结构而不是文案"的宽容度，代价是**漏判**：一个字段建对了类型但语义不对，评测不会发现。这部分仍需人工抽查，评测集只兜住"能不能干活"这一层。

## 实施中发现的两处语义陷阱（已修复）

1. `ConceptField.required` 的默认值必须是 **True**：用例文件里列出的 concept 默认就是"必须有"，只有显式写 `required = false` 才是软提示。默认 `False` 会让所有用例的语义字段检查静默跳过。
2. 检查名用 `description`，否则回退到**第一个** hint（而不是拼接 `hints[:2]`），否则会得到 `account账户` 这种既不可读又难断言的标签。
