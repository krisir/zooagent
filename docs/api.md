# HTTP 与流式协议

所有接口位于 `/api` 下。除 `/api/health` 外都需要会话 token：

```
Authorization: Bearer <token>       # 或 ?token=<token>
```

Token 由后端启动时生成（桌面壳从 stdout 握手读取），开发模式固定为 `dev-token` 并由 Vite 代理注入。

## 系统

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 无需鉴权 |
| GET | `/api/workspace` | 版本、工作空间路径、当前模型、是否离线 |
| GET | `/api/workspace/overview` | 主助手数据页：助手目录 + 每个助手的模型/操作/页面数量（只有结构，不含业务数据） |

## 应用与助手

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/applications` | 应用列表 |
| POST | `/api/applications` | `{name, description?, icon?, color?}` |
| GET | `/api/applications/{id}` | 概览：`{application, agent, models, actions, ui}` |
| PATCH | `/api/applications/{id}` | 改名/描述/状态 |
| DELETE | `/api/applications/{id}` | 软删除（数据文件保留） |
| GET/PATCH | `/api/applications/{id}/agent` | 应用助手配置（名称/指令/开场白） |

## 模型与数据

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/applications/{id}/models` | 模型定义列表 |
| POST | `/api/applications/{id}/models` | 按 key upsert（返回 `created`/`version`） |
| GET | `/api/applications/{id}/models/{key}` | 单个模型 |
| DELETE | `/api/applications/{id}/models/{key}` | 软删除模型 |
| POST | `.../models/{key}/query` | 查询 DSL |
| POST | `.../models/{key}/aggregate` | 聚合 |
| POST | `.../models/{key}/records` | `{values}` 新增 |
| GET | `.../models/{key}/records/{record_id}` | 单条 |
| PATCH | `.../models/{key}/records/{record_id}` | `{values, expected_revision?}` |
| DELETE | `.../models/{key}/records/{record_id}?hard=false` | 软删除 |
| POST | `.../models/{key}/records/{record_id}/restore` | 恢复 |
| GET | `.../models/{key}/records/{record_id}/history` | 逐版本历史 |

## Action 与界面

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/applications/{id}/actions` | Action 列表 |
| POST | `/api/applications/{id}/actions` | 按 key upsert |
| GET | `/api/applications/{id}/actions/{key}` | 单个 Action |
| POST | `/api/applications/{id}/actions/{key}/run` | `{input}` → `{output, trace, effects}` |
| DELETE | `/api/applications/{id}/actions/{key}` | 软删除 |
| GET | `/api/applications/{id}/ui` | 页面定义 |
| PUT | `/api/applications/{id}/ui` | 整体替换页面（写入时校验引用） |

## 对话

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/chat/primary` | 主助手对话（流式） |
| GET | `/api/chat/primary` | 主助手会话 + 消息 |
| POST | `/api/applications/{id}/chat` | 应用助手对话（流式） |
| GET | `/api/applications/{id}/chat` | 应用助手会话 + 消息 |
| POST | `/api/conversations/{id}/reset` | 开始新会话（保留旧会话） |

请求体：`{"message": "...", "conversation_id": "conv_..." | null}`

### 流式格式

响应为 `application/x-ndjson`：每行一个 JSON 事件。

```json
{"type": "start", "conversation_id": "conv_…"}
{"type": "tool_call", "name": "define_model", "arguments": {...}}
{"type": "tool_result", "name": "define_model", "ok": true, "result": {...}, "error": null, "refresh": true}
{"type": "assistant_note", "content": "..."}
{"type": "agent_message", "direction": "out", "ok": true, "peer": {"name": "主助手"},
 "request": "把助手改得更温柔一点", "reply": "方案：…", "conversation_id": "conv_…"}
{"type": "assistant_message", "content": "已经为你创建…", "tools": [...], "refresh": true}
{"type": "error", "message": "…"}
{"type": "done", "conversation_id": "conv_…", "message_id": "msg_…", "refresh": true, "errored": false}
```

`refresh: true` 表示应用结构或业务数据发生变化，前端应刷新列表/表格。

`agent_message` 只在助手之间转达消息时出现：应用助手把结构类诉求转给主助手（或反过来），
对方答完后平台把答复记进两边对话，并把发送方这张折叠卡片推给前端。
被联系的主助手是只读的，只会给方案；真正的改动要用户在**主助手对话**里确认。

客户端点「停止」即断开这个流：服务端取消本轮 Agent（不再继续调用模型/工具），
已写入的用户消息保留，这一轮的助手回答不写入会话历史。

## 设置

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/settings` | 模型配置（密钥只返回是否已设置）+ 提供方预设 |
| POST | `/api/settings/profiles` | 新增/更新模型配置（`api_key` 传 `***` 或空表示不改） |
| POST | `/api/settings/profiles/{id}/activate` | 切换当前模型 |
| DELETE | `/api/settings/profiles/{id}` | 删除 |
| POST | `/api/settings/test` | 发一次最小请求验证连通性 |

## 错误响应

```json
{"error": {"code": "validation_error", "message": "…", "details": [...]}}
```

`details` 在字段校验失败时给出 `{field, reason}` 或界面引用问题列表，便于前端和模型自我修正。
