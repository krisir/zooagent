# 架构总览

## 分层

```
React + TypeScript            工作台、应用视图、声明式 UI 渲染器、对话
        │  HTTP + NDJSON
FastAPI                       应用/模型/数据/Action/UI/对话接口
        │
Primary Agent / App Agent     provider 无关的工具调用循环（LLM 只产出结构化调用）
        │  Tool Registry
Model Engine  Data Engine  Action Engine  UI Schema Service  Application Registry
        │
SQLite                        platform.db + 每个应用一个 data.db
```

分层的硬约束：

1. **LLM 只做理解与设计**。它调用工具写出模型、Action、页面定义；执行路径上没有 LLM。
2. **引擎是唯一写入通道**。REST、Action、Agent 工具共用同一套校验与事务。
3. **UI 只是声明的渲染结果**。AI 不生成 React/HTML/JS，页面定义在打开时直接渲染。

## 请求路径

### 主助手创建应用

```
POST /api/chat/primary
  → AgentRuntime: system prompt + 历史 + 用户消息
  → LLM 返回 tool_calls（create_application / define_model / ...）
  → ToolRegistry 执行工具（Pydantic 校验 → 引擎 → 事务）
  → 工具结果回填给模型，直到模型不再调用工具
  → 流式回传 tool_call / tool_result / assistant_message / agent_message / done
```

`done` 事件带 `refresh: true`，前端据此刷新应用列表、窗口结构和表格数据。

### 用户操作数据

```
DataTable → POST /models/{key}/query（安全 DSL，不是 SQL）
StatCard  → POST /models/{key}/aggregate
按钮      → POST /actions/{key}/run（确定性工作流，事务）
表单      → POST/PATCH /models/{key}/records
```

## 关键设计

### 模型与物理表解耦

所有应用共享固定的物理表（`records` / `record_relations` / `record_unique_keys` / `record_history`）。
业务模型是元数据（`models` + `model_versions`），记录值是 `records.data_json`。

因此：新增字段不需要 DDL、不重写历史行；模型升级只产生新版本行；删掉字段后旧数据仍保留（不再展示）。

### 查询安全

模型只能提供 `{field, operator, value}`。字段 key 先按模型白名单校验，再拼进 `json_extract` 路径；所有值都走参数绑定。聚合只允许 `count/sum/avg/min/max` + 单层 `group_by`，不支持任意 JOIN。

### 事务与并发

- 写操作在调用方的 `with conn:` 事务内执行；Action 失败整组回滚。
- 记录带 `revision`，客户端可传 `expected_revision`，不匹配返回 409（避免 AI 与用户互相静默覆盖）。
- 唯一约束落在 `record_unique_keys` 上（数据库唯一索引），文本不区分大小写并去空格。
- 软删除保留数据与历史；关系标签会标注「（已删除）」。

### Action 表达式

Action 的 `values` 支持三种形式：字面量、`{"ref": "input.amount"}`、`{"expr": "$input.balance - $input.amount"}`。

表达式由自研的安全求值器处理：只允许字面量、变量、下标、算术、比较、布尔、条件表达式和白名单函数（`sum/avg/min/max/round/coalesce/pluck/...`），不调用 `eval`，不允许属性访问任意对象或导入模块。

### Agent 运行时

运行时由模型配置的 `provider` 决定，两条路径共享同一套工具与引擎：

| provider | 运行时 | 说明 |
| --- | --- | --- |
| `agentscope` | AgentScope `Agent`（ReAct） | 推理循环、工具协议、模型接入均由 AgentScope 2.0 负责（ADR-0002） |
| `openai` / `ollama` | 自研轻量循环 | 无额外依赖，便于分发 |
| `mock` | AgentScope 接口的 mock 模型 | 真实 AgentScope 链路 + 确定性与测试数据（ADR-0006） |
| `offline` | 内置模板规划器 | 无密钥也能走完验收闭环（ADR-0005） |

自研运行时的循环：模型返回 `tool_calls` → 注册表执行 → 结果回填。工具结果以 `{ok, result|error}` 回传，模型可以看到校验错误并自行修正。

接入 AgentScope 时的桥接要点（都是实测出来的契约）：

- `Toolkit.add_tool` 是协程，桥接需要 `await`。
- 自定义模型必须暴露 `formatter`（Agent 会读 `supported_input_media_types` 校验入参）。
- 工具需显式返回 `PermissionBehavior.ALLOW`，否则默认权限模式会等待用户确认。
- `ToolCallBlock.input` 是 JSON **字符串**；`ToolCallDeltaEvent` 分片需要累积后再解析。

工具集合按作用域构建：

- 主助手：应用/模型/Action/页面/种子数据/测试数据 + 只读查询。
- 应用助手：绑定单一 `application_id`，只有该应用的模型与 Action。

### 桌面集成

Rust 壳只做三件事：启动 sidecar、读取 `ready` 握手（端口 + token）、窗口关闭时结束进程。它不参与业务逻辑，因此 Windows/Linux 上只需替换打包流程。

两个实现细节值得记住（都是实测踩出来的）：

1. **stdout 只用于握手**。后端在 stdout 上只打印一行 `{"event":"ready",...}`；所有诊断信息走 stderr。壳会逐行读取并只接受 `event == "ready"` 的那一行，因此将来增加日志不会破坏启动。
2. **父进程看门狗**。打包后的 sidecar 是 PyInstaller onefile，壳 `spawn` 到的是引导父进程，真正的服务是它的**子进程**；壳只杀父进程，子进程会变成孤儿继续占用端口。因此壳通过 `ZOOAGENT_PARENT_PID` 把自己的 pid 传给服务，服务内的守护线程每 2 秒检查一次，发现壳消失就立刻退出（**先退出，再尝试打印日志**——此时 stdout 管道已经关闭，打印会抛 `BrokenPipeError`）。

## 工作空间

```
~/Library/Application Support/zooagent/        (macOS)
├── config/settings.json                       模型配置（0600）
├── system/platform.db                         应用注册表、助手、会话、消息、审计
├── applications/<app_id>/
│   ├── application.json                       人可读的完整快照（模型/Action/界面）
│   ├── data.db                                业务数据
│   ├── attachments/ backups/ snapshots/
├── logs/
└── runtime/session.json                       开发期会话信息
```

路径通过 `ZOOAGENT_HOME` 覆盖；Windows/Linux 使用系统数据目录，不硬编码 macOS 路径。

## 错误模型

| 场景 | HTTP | code |
| --- | --- | --- |
| 字段/模型/界面定义不合法 | 422 | `validation_error` |
| 记录不存在 | 404 | `not_found` |
| revision 冲突、唯一键冲突 | 409 | `conflict` |
| 未授权 | 401 | — |
| 模型服务失败 | 502 | `provider_error` |

工具调用失败不会中断对话：错误以结构化结果返回给模型，由模型决定修正或向用户解释。
