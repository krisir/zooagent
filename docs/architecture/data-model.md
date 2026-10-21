# 领域与数据模型

## 实体

| 实体 | 存储 | 生命周期 |
| --- | --- | --- |
| Workspace | 工作空间目录 | 长期 |
| Application | `platform.db: applications` | 创建 / 升级 / 删除 |
| Agent | `platform.db: agents` | 主助手一个；每个应用一个 |
| Conversation / Message | `platform.db: conversations / messages` | 追加 |
| BusinessModel | `data.db: models + model_versions` | 版本化 |
| Record | `data.db: records` | CRUD + 软删除 |
| Action | `data.db: actions + action_versions` | 版本化 |
| UIPage | `data.db: ui_pages` | 整体替换（版本随应用） |
| RecordHistory | `data.db: record_history` | 追加 |
| AuditLog | `platform.db: audit_logs` | 追加 |

`Application.key`（模型 key / Action key）是稳定引用；`name` 可随意改名，`id`（ULID，带类型前缀）永不变。

## 字段类型

| 类型 | 存储形式 | 校验 |
| --- | --- | --- |
| text | 字符串 | 非空（required 时）；长度上限 |
| integer | 整数 | 可解析为整数 |
| decimal | **字符串**，按 scale 量化（`"12.35"`） | 有限数字；避免浮点误差 |
| boolean | JSON 布尔 | 接受 true/false、"是/否"、0/1 |
| date | `YYYY-MM-DD` | `date.fromisoformat` |
| datetime | `YYYY-MM-DDTHH:MM:SSZ` | 统一转 UTC |
| enum | 选项值字符串 | 必须在 `options` 内 |
| relation | 目标记录 id（或 id 列表） | 目标记录必须存在且未删除 |
| json | 对象/数组 | 不能是标量 |

未实现（见 README「后续」）：`file`（附件）、`computed`（计算字段，目前用 Action 或界面聚合表达）。

## 记录存储

```sql
records(id, model_id, data_json, revision, created_at, updated_at, deleted_at)
record_relations(model_id, from_record, field_key, to_record)
record_unique_keys(model_id, field_key, value, record_id)   -- UNIQUE(model_id, field_key, value)
record_history(id, model_id, record_id, revision, change_type, data_json, created_at, actor)
```

- `data_json` 只包含当前模型版本的字段（写入时校验，未知字段直接报错）。
- `revision` 从 1 开始，每次写入 +1；`record_history` 保留每个 revision 的完整快照（created/updated/deleted/restored）。
- `record_unique_keys` 是唯一约束的真实来源：文本统一小写+去空格，布尔归一为 1/0，relation/json 不建唯一键。
- 关系双向可查：`from_record` 查引用，`to_record` 查被引用。

## 模型版本

```json
{
  "key": "transaction",
  "name": "交易",
  "title_field": "note",
  "version": 3,
  "fields": [
    {"id": "fld_...", "key": "amount", "name": "金额", "type": "decimal", "scale": 2, "required": true}
  ]
}
```

- `key` 不可修改（Action / 界面按 key 引用）。
- 字段按 key 合并：同名保留原 `id`（引用稳定），新 key 生成新 `id`。
- `define_model` 是 **upsert + 合并**：只提交新增或要修改的字段定义，已有字段不会被删掉。
- 删除字段不会删数据；删除 relation 字段会清理对应的 `record_relations` 行。

## 查询 DSL

```json
{
  "filters": [{"field": "level", "operator": "eq", "value": "vip"}],
  "filter_logic": "and",
  "sort": [{"field": "balance", "direction": "desc"}],
  "limit": 20,
  "offset": 0,
  "expand": true
}
```

操作符：`eq ne gt gte lt lte contains startswith endswith in between is_null not_null`。
`sort` / `filters` 也接受系统列：`id`、`created_at`、`updated_at`、`revision`。

聚合：

```json
{
  "filters": [{"field": "direction", "operator": "eq", "value": "expense"}],
  "group_by": ["date"],
  "granularity": "month",
  "aggregations": [{"function": "sum", "field": "amount", "alias": "total"}]
}
```

`granularity`（`day`/`month`/`year`）只对日期字段生效，把 180 天的记录压成 6 个月度点，用于趋势图；对非日期字段使用会直接报错。

`expand: true` 会给记录附加 `labels`，把关系字段解析成可读标题（已删除目标标注「（已删除）」）。

## Action 定义

```json
{
  "key": "record_transaction",
  "name": "记录一笔交易",
  "inputs": [{"key": "amount", "name": "金额", "type": "decimal", "required": true}],
  "steps": [
    {
      "id": "create_transaction",
      "type": "create",
      "model": "transaction",
      "output": "record",
      "values": {"amount": {"ref": "input.amount"}}
    }
  ]
}
```

节点类型：`query`、`aggregate`、`create`、`update`、`delete`、`compute`、`return`。
变量上下文：`input.*`、`steps.<output>.*`、`now`、`app.name`。
执行返回 `{output, trace, effects}`，`effects` 列出被创建/修改/删除的记录，供界面提示与审计。

## UI Schema

```json
{
  "home_page": "overview",
  "pages": [
    {
      "key": "overview",
      "title": "概览",
      "components": [
        {"type": "StatCard", "model": "transaction",
         "props": {"aggregate": {"function": "sum", "field": "amount"},
                   "filters": [{"field": "direction", "operator": "eq", "value": "expense"}],
                   "period": "this_month", "date_field": "date", "format": "currency"}},
        {"type": "DataTable", "model": "transaction",
         "columns": [{"field": "date"}, {"field": "amount"}],
         "actions": [{"label": "新增交易", "kind": "run_action", "action": "record_transaction"}]}
      ]
    }
  ]
}
```

组件：`Heading` `Text` `Divider` `DataTable` `Form` `DetailView` `StatCard` `Chart` `Calendar` `Tabs` `ActionButton` `FilterPanel` `ChatPanel`。

写入时校验：模型存在、字段存在、Action 存在、跳转页面存在。这样用户不会打开一个"打开即报错"的页面。

`period` 由渲染器在本地展开为 `between` 过滤条件（`this_month` / `this_year` / `all`），因此"本月支出"会随时间自动更新，不需要 AI 每次重新生成。
