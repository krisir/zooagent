# 代码审查与性能分析报告（2026-10-21）

审查范围：`backend/src/zooagent`（约 12.9k 行）、`apps/desktop/src`（约 3.3k 行）、
`apps/desktop/src-tauri/src`、`scripts/`。约束：**不改变业务逻辑**。

基线状态：

| 项目 | 结果 |
|---|---|
| `uv run pytest -q` | 94 passed |
| `uv run ruff check src`（默认 E/F/I/UP/B） | 0 告警 |
| `uv run ruff check --select E,F,I,UP,B,SIM,C4,PERF,RUF,PL,ASYNC,S,DTZ,PT,ARG,RET,TRY` | 945 条（其中 585 条为中文标点 `RUF001`、114 条 `TRY003` 属风格噪音） |
| `tsc --noEmit`（strict + noUnusedLocals） | 通过 |

实测手段：临时工作空间内直接驱动 `ModelEngine` / `DataEngine` / `MockData`
并统计 SQL 语句数与耗时；`EXPLAIN QUERY PLAN` 验证索引使用情况。

---

## 一、正确性与安全问题（建议优先处理）

### S1. 聚合别名直接拼进 SQL（注入面）— `data/engine.py:108-114`
```python
alias = agg.output_name                       # Aggregation.alias 无任何校验
select_parts.append(f"{build_aggregation(model, agg)} AS {alias}")
```
同一模块里 `filter` / `sort` / `group_by` 的字段全部经 `field_expr()` 白名单校验，
**只有 `alias` 例外**：`Aggregation.alias` 是裸 `str | None`。
实测把 alias 写成 `c FROM records WHERE 0 UNION SELECT ...` 时，语句会被原样拼入 SQL 并送达 SQLite
（返回 `Incorrect number of bindings` / `UNION do not have the same number of result columns`
说明注入文本确实进入了 SQL 文本）。

攻击面：`define_action` / `aggregate` 的聚合定义由 LLM 生成，落库后由按钮或工具执行，全程无人审阅 SQL。

**修复**：`alias` 复用 `models.schema.KEY_PATTERN` 校验（合法标识符）。正常定义的别名（如 `total`）
不受影响，非法别名一律 `ValidationFailure`。

### S2. 会话令牌以 0644 落盘 — `state.py:73-86`
`write_session()` 把 token 写入 `runtime/session.json`，没有 chmod；而 `config.py:195`
的 `settings.json` 反倒做了 0600。本机任意进程可读该文件，loopback 令牌失去意义。
另外写入不是原子写（`_write_manifest` 反而用了 tmp+replace）。

**修复**：tmp+`replace` 原子写 + 0600。

### S3. 工具结果按字节截断的 JSON 回灌模型 — `agents/runtime.py:120-130`
```python
text = json.dumps(result, ...)
return text[:20_000]        # 可能切成非法 JSON
```
`query_records(limit=1000)` 很容易超预算。模型收到半截 JSON 会当作数据继续推理 → 幻觉 / 无谓重试。

**修复**：先压缩**载荷**（`items` 保留尽可能多但保证总量在预算内，其余降级为 preview），
再序列化，保证回灌的始终是合法 JSON，并附 `truncated: true` 提示模型缩小范围。

### S4. 用 404 表达参数错误 — `api/models.py:118`
`raise NotFoundError("记录内容必须是对象")` → 客户端拿到 404。应为 `ValidationFailure`（422）。

### S5. 删除/恢复不校验 `deleted_at` — `data/engine.py:233-285`
`delete_record` 的查询缺少 `deleted_at IS NULL`（`update_record` 有），重复软删会再写一条
`deleted` 审计并 +1 revision；`restore_record` 对未删除记录也返回成功并 +1 revision。
**修复**：重复软删 → `NotFoundError`；重复恢复 → 幂等返回当前记录（不写审计、不 +revision）。

### S6. 同一时间戳调用两次 — `data/engine.py:259-260`
```python
(utc_now(), revision, utc_now())    # deleted_at 可能 > updated_at
```
毫秒边界上审计不自洽。**修复**：取一次变量。

### S7. `$` 反糖会改写字符串字面量 — `actions/expression.py:152`、`actions/validation.py:177`
`re.sub(r"\$([A-Za-z_])", r"\1", expr)` 是全局文本替换，实测 `str('$abc')` → `'abc'`。
改动会改变存量表达式的求值结果，本轮**不动**，仅记录为已知限制（建议以后在 AST 层做兼容）。

### S8. Tauri webview `csp: null` — `apps/desktop/src-tauri/tauri.conf.json`
页面渲染 LLM 输出（自研 Markdown 渲染器不注入 HTML，风险有限），但没有 CSP 就少最后一层防线。
Tauri 的 `app.security.csp` 只对打包进 `frontendDist` 的 HTML 生效，而本项目 HTML 由 Python
sidecar 提供（`WebviewUrl::External`），因此**不能靠改 tauri.conf.json 生效**；
正确做法是在 FastAPI 静态服务上回 CSP 头。因无法在本机验证打包后的 webview，
本轮**不动**，仅记录（建议的头部见文末「遗留项」）。

---

## 二、规范性问题

| # | 位置 | 问题 | 处理 |
|---|---|---|---|
| Q1 | `agents/tools/registry.py:172-197` | `failed_action_runs()` 被放在 `# JSON schema helpers` 分隔线之后，夹在模块级函数前 | 移回 `ToolRegistry` 方法区 |
| Q2 | `agents/runtime.py:130` | `20_000` 魔法数字 | 具名常量 `MAX_TOOL_RESULT_CHARS` |
| Q3 | `api/chat.py:102-286` | 单个生成器 ~180 行、分支 10+ | 本轮只做低风险整理，不重构（缺测试保护） |
| Q4 | `api/settings.py:191-250` | `asyncio.run` 在同步 endpoint；if/else 两分支定义**同名**内部函数 `_probe_agentscope` | 拆成 `_probe_agentscope()` / `_probe_builtin()` |
| Q5 | `api/models.py:196-201` | 扁平 payload 下静默丢弃 `expected_revision`，且未校验类型（字符串 → 永远 409） | 加 int 校验 |
| Q6 | `models/schema.py:186` | `to_prompt_dict(include_titles=...)` 参数从未使用 | 删除 |
| Q7 | `models/schema.py:168`、`agents/tools/registry.py:76,90`、`api/streaming.py`、`errors.py:55` | 死代码：`field_by_id`、`ToolRegistry.has`、`ToolRegistry.describe`、整个 `api/streaming.py`、`PermissionDenied` | 删除 |
| Q8 | `config.py:195-210` | `save()` 非原子写，写坏后 `load()` 直接抛 `ConfigurationError` 无恢复 | tmp+replace |
| Q9 | `data/engine.py:92-96` | 函数内 `from .query import Aggregation` + 局部变量遮蔽 `spec.aggregations` | 提到模块顶部 |
| Q10 | `models/engine.py:210`、`actions/engine.py:365`、`state.py:75` | 函数内 import（部分为规避循环依赖，部分不是） | 无循环依赖的提到顶部 |
| Q11 | `ui_schema/service.py:91-148` | `_check_component` 21 分支 / 5 参数 | 本轮不动（纯重构，缺测试覆盖） |
| Q12 | 前端 `features/chat/useChat.ts:90,120,139` | `id: local-${Date.now()}` 同毫秒撞 React key | 递增计数器 |
| Q13 | 前端 `components/ChatPanel.tsx:135` | `onPressEnter` 未判断 `isComposing`，中文输入法回车选词会误发 | 加 `!event.nativeEvent.isComposing` |
| Q14 | 前端 `components/Renderer.tsx:621-624` | 表内搜索每个按键都发请求 | 250ms debounce |
| Q15 | 前端 `useChat.ts:110-118` | `tool_result` 靠「最后一个 pending」推断对应哪次调用，乱序即错配 | 后端事件补 `id`，前端按 id 匹配 |

---

## 三、性能问题（含实测）

### P1. `query_records` 的 N+1（最高收益）— `data/engine.py:58-85, 287-341`
每一行、每一个 relation 字段都要：
1. `ModelEngine.get_model_optional()` → 一次 JOIN + `json.loads` + `ModelDef.model_validate()`（实测 0.034 ms/次）；
2. 再发一条 `SELECT ... WHERE id IN (...)` 取标题。

| 行数 | SQL 条数 | 耗时 |
|---|---|---|
| 50（1 个 relation） | 102 | 9 ms |
| 300（2 个 relation） | **1202** | **45 ms** |

**修复（结果完全一致）**：单次查询内缓存已解析的模型；relation label 按字段**批量**取
（一次 `IN` 覆盖该字段所有行，并按 900 个参数分块，顺带修掉「多选关系 > 999 个 id 直接报错」的隐患）。
预期 4 条 SQL、个位数毫秒。

### P2. 分页查询每次都 `TEMP B-TREE` 排序 — `persistence/sqlite.py:190`
```
SEARCH records USING INDEX idx_records_model (model_id=? AND deleted_at=?)
USE TEMP B-TREE FOR ORDER BY           ← 对全量命中行排序
```
`idx_records_model(model_id, deleted_at)` 缺 `created_at`。
**修复**：索引改为 `(model_id, deleted_at, created_at)`，并为已存在的应用库补一次幂等初始化
（`init_application_db` 只在建应用时跑过，老库需要迁移入口）。

### P3. `messages` 排序不稳定 — `application/conversations.py:196-211`
`ORDER BY created_at`（毫秒精度）并列时顺序未定义，历史/展示顺序可能偶发翻转。
**修复**：`ORDER BY created_at, id`（message id 是 ULID，本身单调且唯一）。

### P4. 阻塞 I/O 跑在事件循环上 — `api/actions_ui.py:63`、`api/chat.py` 流式生成器
`async def` 里做同步 SQLite + 可能上百毫秒的 Action，会阻塞其它请求与流式输出。
单用户桌面基本无感，本轮**不动**（改动涉及 streaming 语义，需真机验证）。

### P5. 每请求 3–4 个 SQLite 连接 — `application/service.py:352`
`open_app()` = `get_application()` + `get_agent()` + `connect()`；一次助手回合 5 个工具 ≈ 15 个连接。
`connect()` 仅 0.13 ms，非瓶颈，本轮**不动**（与 P1 的模型缓存叠加后收益才明显）。

### P6. 前端请求扇出
- 一页 6 个 StatCard + 2 个 Chart + 1 个 Table = 9 个独立请求；antd `Tabs` 中访问过的页签仍挂载，
  `refreshToken` 一变所有挂载页一起重拉（`app/ApplicationView.tsx:120`）。
- `components/Markdown.tsx:169` 每次 Thread 渲染都重新 parse 全部历史消息（流式时每个 tool step 都触发）。
- **修复**：`Markdown` 用 `React.memo` 包裹；`Thread` 的可见消息列表 `useMemo`。（批量端点属新增 API，本轮不做。）

### P7. 其它较小项
| 位置 | 现状 | 处理 |
|---|---|---|
| `models/engine.py:196` `rebuild_indexes` | 每升一版都把整表 unique key 全删重建（1500 行 → 1507 语句 / 7 ms） | unique 字段集合未变则跳过 |
| `agents/mock_data.py:300-410` | 200 条 → 2405 条语句（每条 `create_record` 都回读一次记录并重算 labels） | 本轮不动 |
| `data/engine.py:70-77` | `COUNT(*)` + 分页 SELECT 两次扫描 | 本轮不动（语义边界多） |
| `components/Renderer.tsx:1124-1140` | 日历 42 格 × N 条记录每次都 `filter+startsWith` | 预聚合 `Map<date, count>` |

---

## 四、处理顺序与结果

1. **安全**：S1 别名校验、S2 session.json 权限 → 已修复；S8 CSP 见遗留项。
2. **正确性**：S3 合法截断、S4 错误类型、S5 删除/恢复、S6 时间戳、Q12-Q15 → 已修复；S7 记录为已知限制。
3. **性能**：P1 批量/缓存、P2 索引+迁移、P3 排序、P6 前端 memo、P7 rebuild/日历 → 已修复；P4/P5 保留。
4. **整理**：Q1-Q2、Q4-Q10 → 已修复；Q3、Q11 保留（需要测试保护再重构）。

修复后的验证：`pytest`、`ruff check src`、`tsc --noEmit`、以及 P1 的 SQL 条数/耗时回归。

## 五、遗留项（需要在真机上人工验证）

| 项 | 建议 |
|---|---|
| S7 `$` 反糖 | 在 AST 层做 `$name` 兼容，避免改写字符串字面量；改前先加表达式用例 |
| S8 CSP | 在 FastAPI 上回 `Content-Security-Policy`：`default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self' ws: wss:; font-src 'self' data:`（antd cssinjs 需要 `style-src 'unsafe-inline'`），打包后逐一验证页面/图表/设置弹窗 |
| token 查询参数 | `api/deps.py` 仍接受 `?token=`（`tests/integration/test_api.py:60` 显式断言支持）。令牌进 URL 会被日志/历史记录，建议下个大版本改为只认 header，并同步改测试 |
| P4 阻塞 I/O | 把同步引擎调用放进 `run_in_threadpool`，或把只读 endpoint 改成 `def`（FastAPI 自动走线程池） |
| P5 连接复用 | 引入「按请求的 app 上下文缓存」，与 P1 的模型缓存合并做 |
| `api/chat.py` 生成器 | 抽 `_emit_relays()` / `_emit_diagnoses()`，先补集成测试再重构 |
