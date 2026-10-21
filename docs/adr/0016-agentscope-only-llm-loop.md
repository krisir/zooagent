# ADR-0016 真实 LLM 只有 AgentScope 一条循环

- 状态：**已实施**
- 日期：2026-10-21
- 取代：ADR-0002 里「`openai` / `ollama` 保留自研轻量运行时」的部分

## 背景

第一版为了把数据内核和生成协议做稳，先写了自研的 provider 无关工具调用循环（`agents/runtime.py` 的 `AgentRuntime`）+ 一个 OpenAI 兼容 HTTP 客户端（`agents/llm/openai_compatible.py`）。ADR-0002 接入 AgentScope 之后，这两条路并存：

- `agentscope` → AgentScope 的 ReAct Agent
- `openai` / `ollama` → 自研循环

并存期间维护成本一直在：两套循环对工具结果回填、迭代上限、异常上报、事件流的处理都要各自对齐；模型能力（多轮工具调用、并行工具调用、上下文压缩）只有 AgentScope 侧能吃到；出问题时还要先判断"用户走的是哪条循环"。

## 决定

**真实 LLM 的推理循环只保留 AgentScope。** 自研循环降级为 `MiniLoop`（`agents/mini_loop.py`），只服务确定性模型：

| provider | 循环 |
| --- | --- |
| `agentscope`（openai / deepseek / moonshot / dashscope / ollama / anthropic 由 `model_family` 区分） | AgentScope `Agent`（ReAct） |
| `offline`（内置模板规划器） | `MiniLoop` |
| `mock` | 装了 extra → AgentScope + `MockChatModel`；没装 → `MiniLoop` + `MockChatModelLite` |
| 测试里的脚本模型 | `MiniLoop` |

判定标准是**模型是不是真 LLM**，而不是"是不是产品功能"：offline 是规则匹配出来的确定性规划器、mock 是脚本化假模型，它们都不需要推理循环，`MiniLoop` 足够；而一旦是真模型，循环、工具协议、上下文管理全部交给 AgentScope。

## 连带改动

- 删除 `agents/llm/openai_compatible.py`（122 行）与 `build_chat_model` 的 openai 分支；`agents/llm/` 只留消息协议 `base.py` 和 `offline.py`。
- `httpx` 从运行时依赖移到 dev 组：自研客户端是它唯一的使用方，生产代码只剩 starlette 测试客户端需要它。
- `agents/runtime.py` → `agents/mini_loop.py`，`AgentRuntime` → `MiniLoop`，`AgentRuntimeProtocol` → `AgentLoopProtocol`。
- `ModelProfile.provider` 收敛为 `agentscope | offline | mock`，默认 `agentscope`。
- 设置页预设去掉「OpenAI 兼容 / DeepSeek / Ollama（内置运行时）」三条，AgentScope 的五条预设本就覆盖同样的厂商。
- 连通性测试 `POST /api/settings/test` 只走 AgentScope 探针。

## 老配置迁移

`SettingsService.load()` 读到 `provider: "openai" | "ollama"` 时就地改写成 `agentscope`（`ollama` 额外补 `model_family: "ollama"`，因为它旧的 `base_url` 带 `/v1`），然后**写回**文件——只迁移一次，之后文件就是新写法。密钥、`base_url`、模型名全部保留，用户不需要重配。

## 代价

- 接真实模型必须装 `--extra agentscope`（打包版一直随包分发；`ZOOAGENT_WITHOUT_AGENTSCOPE=1` 的瘦身包从此只能跑 offline 与 mock 兜底）。缺依赖时报错信息直接给出 `uv sync --extra agentscope`。
- 自研循环不再演进，也不该被拿去接真模型：`MiniLoop` 的文档与命名都写明了它的服务对象。

## 未做

- `MiniLoop` 与 `AgentScopeRuntime` 的事件流仍由各自的实现翻译成同一套 NDJSON；两边的等价性靠 `tests/integration/test_agentscope_runtime.py` 与 `tests/eval` 的用例对齐，没有做自动化的差分测试。
