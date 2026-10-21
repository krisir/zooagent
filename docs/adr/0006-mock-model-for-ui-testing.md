# ADR-0006 Mock 模型：按 AgentScope 接口实现，产出测试数据

- 状态：确定
- 日期：2026-10-20

## 背景

需要同时满足两件事：

1. **测试 UI**：表格、图表、分页、表单只有在几十到几百条真实数据上才能被验证；手点录入不现实。
2. **验证 AgentScope 接入**：真实模型需要密钥与网络，无法在 CI 或离线环境回归。

## 决策

实现一个 mock 模型 `agents/agentscope/mock_model.py:MockChatModel`：

- **直接继承 AgentScope 的 `ChatModelBase`**，实现 `async __call__(messages, tools, ...) -> ChatResponse`，因此 AgentScope 的 `Agent`、`Toolkit`、事件流全部是**真实代码路径**，只有"推理"这一步被替换成确定性规划器。
- 同一个规划器（`MockPlanner`）也提供我们轻量协议的入口（`MockChatModelLite.complete`），所以在未安装 AgentScope 的环境里 mock 模式依然可用。
- 它只通过工具操作平台（`create_application` / `define_model` / … / `generate_mock_data`），不直接写库，因此 mock 与真实模型走的是同一条链路。

### 数据生成

`agents/mock_data.py` 负责把模型定义变成"像真的一样的数据"：

- 按字段类型与字段名/名启发式选中文词库（人名、公司、商品、分类、备注、地址…），而不是 `text_1/text_2`。
- 尊重**真实约束**：枚举取合法值、decimal 按 scale 量化、日期落在过去 180 天内、布尔随机、唯一字段对数据库去重（必要时加后缀）、关系字段从**已存在**的目标记录里挑。
- 收入行金额在 3000-15000、支出行在 8-800，因此"本月支出/收入"卡片和分类饼图看起来都合理。
- 固定 `seed` ⇒ 可复现的同一批数据。
- 生成数据的入口是**应用助手**（`generate_mock_data` 只注册在应用工具集里）。主助手只建结构、不注册数据工具，所以 mock 模式下新建的应用是空壳，需要再对应用助手说一句「生成 150 条测试数据」。
- 逐条写在自己的 savepoint 里：一条失败不会影响其余记录，最后返回 `{created, failed, errors}` 报告。

模型间按**依赖拓扑序**生成（分类/账户先于交易），可直接对一个应用一键填充。

## 使用方式

| 入口 | 用途 |
| --- | --- |
| 设置里选择「Mock 模式」 | 主助手会创建应用**并自动填充 120 条数据**；应用助手可「给交易生成 200 条测试数据」 |
| `scripts/dev/generate_mock_data.py --workspace <dir> --count 200` | 直接给现有应用灌数据（不需要运行中的应用），用于 UI 测试 |
| `tests/integration/test_agentscope_runtime.py` | 用 mock 模型跑真实 AgentScope ReAct 循环的回归测试 |

## 风险

- mock 的规划器是**关键词 + 结构**驱动的，不是语言理解。它只做内置模板（记账/客户/清单）与数据生成；遇到不匹配的需求会明确提示用户去配置真实模型，不会假装理解（与 ADR-0005 同一条原则）。
- `generate_mock_data` / `generate_mock_records` 是给演示与测试用的工具，描述里已写明用途；真实模型若被用户要求"造数据"也会调用它——这是有意为之，但单次上限 2000 条。
