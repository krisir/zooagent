# ADR-0004 不执行 AI 生成的代码：声明式 Action + 声明式 UI

- 状态：确定
- 日期：2026-10-20

## 决策

AI 只能产出**声明**，平台负责**执行**：

- 业务逻辑 → Action 的确定性步骤（`query/aggregate/create/update/delete/compute/return`）+ 受限表达式（自研 AST 求值器，非 `eval`）。
- 界面 → UI Schema（固定组件白名单），React 渲染器负责渲染。
- 数据访问 → 安全查询 DSL，字段 key 走白名单，值全部参数绑定；绝不执行模型生成的 SQL。

## 理由

1. 普通 CRUD 不应该依赖 LLM（原则 1）：点击"新增交易"必须每次都一样快、一样准。
2. 可审计：每个 Action 有版本、有输入输出契约、有 step trace 与 effects 列表。
3. 可回滚：Action 在一个事务里执行，失败整组回滚。
4. 安全：没有任意代码执行面，也就没有"AI 生成代码"带来的逃逸风险。

## 已知限制

- 复杂业务逻辑需要拆成多个步骤表达；首版不支持循环、子流程、外部 HTTP 节点。
- 已规划但对 AI 不可见的能力：受限 Python Worker（`scripts`）、定时触发（M5）。
