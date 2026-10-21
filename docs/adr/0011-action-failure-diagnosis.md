# ADR-0011 Action 失败后交主助手排查：只诊断，不自动改

- 状态：确定
- 日期：2026-10-20

## 背景

Action 是 AI 生成、按确定性步骤执行的业务操作。生成它的模型可能写出人类一眼能看出的错误
（字段名拼错、引用了后面步骤才产出的结果、创建时漏掉必填字段），define-time 校验只能挡住
一部分（例如 `validate_action` 用全局 outputs 集合，前向引用能通过校验、每次运行才失败）。

过去这类失败只表现为：

- 应用页面上一个红色 toast；
- 应用助手把 `ok=false` 转述成"失败了"。

用户看到的是"操作没成功"，看不到"为什么"，也拿不到修复方案；而修 Action 属于结构操作，
应用助手按权限边界根本不能做（ADR-0002 / ADR-0014）。创建这个 Action 的主助手才是能修它的人。

## 决策

### 1. 引擎把失败变成可诊断的快照

`ActionEngine.run` 把所有执行期异常（含输入校验）包装成 `ActionExecutionError`，`details` 里带：

- 应用 id / 名称、Action 完整定义（key、version、steps）、本次输入；
- 失败步骤 id / type / index、执行 trace（含每一步的 ok/error）；
- 已产生但随事务回滚的 effects，以及 `rolled_back: true`。

输出变量的解析也被移入同一个事务：以前 output 引用坏掉会在提交之后才报错，数据已经写进去了。

### 2. 先跑确定性检查，再（可选）问主助手

`actions/diagnostics.py` 拿快照和当前模型定义做机械比对，不需要模型：

- 步骤引用的模型是否存在；筛选/排序/分组/聚合/字段值里的字段 key 是否存在；
- 前向引用（`steps.x` 在产出它的步骤之前被引用）、无法解析的 ref/expr；
- create 步骤缺少必填字段、enum 字面量不在选项里、update/delete 缺 `record_id`；
- 根据错误信息归类（定义问题 / 输入数据问题 / 冲突等）。

字段名拼错且只有一个明显候选时，产出一个**打补丁后的完整 Action 定义**（改 key，不改结构），
所以离线模式也能给出可确认的修复，而不是只有一段解释。

配置了真实模型时，再用主助手做一次**只读**排查（`get_application_overview` / `list_applications`，
`max_iterations=6`，90 秒超时）。它只能看结构，不能调 `define_action`，也不能读写业务数据。
模型给出的 `ACTION_JSON` 会被解析、校验、并要求 key 与失败 Action 一致，才作为候选修复。

### 3. 留痕：主助手对话 + 应用助手转述

- 主助手会话追加一条 `assistant` 消息（metadata `kind=action_diagnosis`），包含自动检查、
  主助手结论、修复建议，以及完整候选定义。它成为主助手后续对话的历史，所以用户下一句
  "按建议修" 时，模型能看到完整 JSON 并调用 `define_action`。
- 应用助手会话追加一条短转述（`kind=action_diagnosis_relay`）：原因 + 已回滚 + 去主助手确认。
- 对话流里在应用助手回答之后追加 `diagnose_action` 工具事件和第二段 `assistant_message`，
  让用户当场看到"已交给主助手排查"，而不是刷新页面才发现。

### 4. 不自动修复

即使确定性检查或模型都给出了候选定义，也不会自动发布。原因：

- 已发布的 Action 可能被页面按钮、应用助手、定时任务引用，自动换版本的影响面比一次失败更大；
- 模型给出的"修复"可能只是掩盖错误（例如把必填字段删掉）；
- 本项目已经有"结构变更走主助手对话 + 用户确认"的既有约定（ADR-0014）。

用户确认后走正常的 `define_action` 路径（校验、版本化、历史数据保留）。修复本身不重试原操作：
写操作重试需要用户重新确认输入。

## 代价与边界

- **按钮路径是异步的**：`POST /actions/{key}/run` 立即返回 422（带失败快照和
  `diagnosis.status=pending`），诊断在后台跑。页面 toast 只提示"去对话里看结论"，不等待模型。
- **对话路径会增加延迟**：应用助手回答完之后才诊断，流要等诊断结束才发 `done`。离线模式是毫秒级；
  真实模型最多 90 秒，UI 上显示"主助手排查失败原因"的进度标签。
- **离线/ mock 不调模型**：只输出自动检查结果，并在报告里说明"当前模型未配置"。
- **诊断可能不准**：自动检查只覆盖机械性错误；语义错误仍依赖主助手（或用户）判断。
  报告里明确区分"定义问题 / 输入数据问题"，避免把所有失败都归咎于 Action。
- **不新增失败审计表**：留痕就是两条对话消息（metadata 里有完整快照）。
  如果以后需要跨会话的失败统计，再引入独立的失败记录，而不是从聊天记录里反查。

## 验证

- `backend/tests/unit/test_action_diagnosis.py`：失败快照完整、事务回滚、拼写错误检测与补丁、
  前向引用检测、输入校验归类、主助手报告与应用转述落库、registry 只诊断"最后一次仍失败"的调用。
- `backend/tests/integration/test_action_diagnosis_flow.py`：应用助手回答 → `diagnose_action` 事件 →
  转述气泡 → 两边会话都留痕。
- `backend/tests/integration/test_api.py`：页面按钮路径返回 `action_failed` 快照与 `diagnosis.pending`，
  后台报告确实写入主助手会话。
- `pytest` 全绿、`ruff check` 通过、`pnpm exec tsc --noEmit` 通过。
