# ADR-0002 AgentScope 作为 Agent 运行时与 LLM 接入层

- 状态：**已实施**；2026-10-21 收敛为「真实 LLM 只有 AgentScope 一条路」（见 ADR-0016）
- 日期：2026-10-20（更新）

## 背景

《设计文档》ADR-004 规定使用 AgentScope 2.0 作为 Agent Runtime 和 LLM 接入层。

第一版实现先用了一个自研的轻量运行时（provider 无关的工具调用循环），把数据内核与生成协议做可靠；接口隔离在 `ChatModel` 协议上，因此接入 AgentScope 不需要改动引擎与工具层。

## 现在的实现

已接入 **AgentScope 2.0.10**，作为可选依赖（`uv sync --extra agentscope`，不引入 torch/transformers）：

| 关注点 | 实现 |
| --- | --- |
| LLM 接入 | `agents/agentscope/models.py`：ModelProfile → AgentScope Credential + ChatModel（OpenAI / DeepSeek / Moonshot / DashScope / Ollama / Anthropic） |
| Agent 运行时 | `agents/agentscope/runtime.py`：AgentScope `Agent`（ReAct）+ `ReActConfig(max_iters=...)`，由 AgentScope 负责推理循环 |
| 工具协议 | `agents/agentscope/toolkit.py`：zooagent `ToolRegistry` → AgentScope `Toolkit` / `FunctionTool` |
| 事件流 | AgentScope 的 `reply_stream` 事件（ToolCall/ToolResult/TextBlock Delta/ExceedMaxIters/ReplyEnd）翻译为桌面端既有的 NDJSON 事件 |
| 消息转换 | `agents/agentscope/convert.py` 集中处理 Msg/Block 之间的转换 |

选用哪个运行时由模型配置的 `provider` 决定：

- `agentscope` → AgentScope ReAct Agent（推荐，ADR-004 的方向）
- ~~`openai` / `ollama` → 保留自研轻量运行时~~ → 已删除，改用 AgentScope（ADR-0016）
- `offline` / `mock` → 见 ADR-0005 / ADR-0006

工具集合、校验、事务、界面渲染在所有路径上完全一致，因为 AgentScope 调用的就是同一批工具。

## 实施中发现的三处 AgentScope 契约（已固化在代码与测试里）

1. `Toolkit.add_tool` 是**协程**，桥接函数必须 `await`。
2. Agent 会读 `model.formatter.supported_input_media_types` 做入参校验，因此自定义模型必须暴露 `formatter`。
3. Agent 默认权限模式为 DEFAULT（每次调用都要用户确认）；zooagent 的工具已由引擎全量校验且平台**不暴露 shell/文件系统能力**（ADR-013），因此桥接时显式返回 `PermissionBehavior.ALLOW`，否则无人值守的运行会卡在确认上。

## 代价

- 打包体积增加（AgentScope 及其依赖）：sidecar 从约 19 MB 增到约 46 MB，`.app` 从约 29 MB 增到约 56 MB。现已**默认随包分发**（`scripts/build/build-sidecar.sh` 默认同步 `--extra packaging --extra agentscope`，可用 `ZOOAGENT_WITHOUT_AGENTSCOPE=1` 退出）；AgentScope 采用惰性导入，因此启动时间不受影响。
- 自研运行时仍然保留，两条路径都需要维护；好在两者共享工具层与引擎，差异仅限"谁来驱动循环"。
