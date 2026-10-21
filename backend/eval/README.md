# 生成质量评测（Eval）

对应设计文档 **13.4 AI 生成质量测试**：用一套**固定的业务需求**去检验 AI 生成的
应用是否正确，而不是靠人工感觉。同一套用例可以横向比较不同模型。

## 用例文件

`eval/cases.toml`。每个用例声明：

| 字段 | 含义 |
| --- | --- |
| `id` / `domain` | 用例标识与所属领域 |
| `prompt` 或 `turns` | 一轮或多轮需求描述 |
| `providers` | 必须通过的 provider 类型：`offline` / `mock` / `llm` |
| `expect` | 结构化期望（见下） |
| `evolution` | 追加需求后必须成立的不变量（多轮用例） |

需求覆盖设计文档点名的五个领域：**家庭记账、客户管理、简单库存、个人物品管理、学习计划与进度**，
另有安全性（提示注入）与模糊需求两个质量用例。

### 期望项（全部可选）

计数类（`{min, max}`）：`model_count`、`total_field_count`、`relation_count`、`action_count`、
`action_step_count`、`page_count`、`data_table_count`、`component_count`、`record_count`、
`tool_calls`、`tool_failures`、`field_names_chinese`。

布尔类：

| 期望 | 检查什么（对应设计文档 13.4 的 ①②③） |
| --- | --- |
| `application_created` | 是否真的建了应用 ① |
| `concept_fields` | 语义字段是否存在，例如「某个 decimal 且名称含金额/amount 的字段」① |
| `relation_targets_resolve` | 关系是否指向存在的模型 ① |
| `actions_valid` | Action 是否有确定性步骤、是否引用真实模型 ② |
| `no_default_on_required` / `enum_options_present` / `keys_valid` | 生成质量与规范 |
| `core_ops_covered` | **每个模型都有页面引用，且存在可录入的表格/表单** ③ |
| `forbidden_key_fragments` | 结构里不应出现 `sqlite`/`drop table`/`;` 等注入痕迹 |

`concept_fields` 用 `type + hints`（hint 匹配字段 key/中文名）而不是精确 key，
因为 key 由 AI 决定；这样用例对模型选择保持宽容，但依然能发现"根本没建金额字段"这类错误。

`evolution` 对第一轮与最后一轮的快照做对比（设计文档 ④⑤）：

- `field_superset`：模型只能增加字段，不能丢字段
- `records_preserved`：旧记录的 id 与**内容摘要**（sha1 前 12 位，每模型最多 300 条）必须仍在
- `version_increased`：模型版本必须前进
- `new_concept_fields`：必须新增符合语义的新字段

## 运行

```bash
cd backend

# 无密钥子集（offline + mock），CI 用
uv run python -m zooagent.eval --provider offline --provider mock

# 真实模型：借用你应用工作空间里已配置的模型（含 API Key）
uv run python -m zooagent.eval --provider llm \
  --workspace "$HOME/Library/Application Support/zooagent"

# 指定某个模型配置 / 单个用例 / 保留工作空间排查
uv run python -m zooagent.eval --provider llm --profile deepseek --only finance-evolution --keep

# 保存结果并与基线比较（换模型、改 prompt 时看指标与状态变化）
uv run python -m zooagent.eval --provider llm \
  --workspace "$HOME/Library/Application Support/zooagent" \
  --json eval/results/2026-10-20-deepseek.json
uv run python -m zooagent.eval --provider llm --compare eval/results/2026-10-20-deepseek.json
```

输出是一张表（用例 / provider / 状态 / 模型数 / 字段数 / 关系数 / Action 数 / 页面数 / 记录数），
失败时附上**具体原因**（例如「这些模型没有任何页面引用: ['loan']」「没有找到金额字段」）。

`--json` 里的 `metrics` 与 `application` 是完整快照，用于跨模型比较：

| 指标 | 说明 |
| --- | --- |
| `models` / `fields` / `relations` / `enums` | 结构规模与关系建模能力 |
| `actions` / `action_steps` | 是否把业务规则固化成 Action ② |
| `pages` / `components` / `data_tables` | 界面是否可用 ③ |
| `records` | 是否真的能开始用 |
| `tool_calls` / `tool_failures` | 工具调用效率与失败率（失败率是最敏感的稳定性指标） |
| `chinese_names` | 中文字段名比例（可用性信号） |
| `duration_s` | 端到端耗时 |

## 与 CI 的关系

`backend/tests/agent_eval/` 把无密钥子集接进了 `uv run pytest`：

- `test_eval_cases.py` 跑 `offline` + `mock` 的用例，并断言五个领域都在用例文件里；
- `test_checks.py` 用合成快照单测**检查器本身**（例如"少建了金额字段"必须失败、"模型丢字段"必须被 evolution 抓到）。

需要密钥的用例（任意领域、追加需求演化、模糊需求）在 pytest 里被跳过，由上面的命令手动跑。
