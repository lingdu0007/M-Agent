# Hello Agent Design

本文档记录 `hello-agent` 框架的设计思路。它不是使用教程，而是回答这些问题：

- 为什么需要这些模块？
- 主流 Agent 框架通常如何抽象？
- 当前实现简化了什么？
- 下一步应该如何演进？

## 1. 设计目标

`hello-agent` 是一个学习型 Agent 框架。它的目标不是一开始就复刻 LangGraph、CrewAI 或 OpenAI Agents SDK，而是把 Agent 运行时的核心结构做清楚。

当前目标：

1. 用少量代码跑通 Agent 主循环。
2. 支持远程大模型和工具调用。
3. 支持本地文件工具和 conversation memory。
4. 保持模块边界清晰，后续可以替换模型、工具、memory 和执行器。

当前非目标：

1. 不做复杂多 Agent 编排。
2. 不做可视化工作流。
3. 不做生产级权限系统。
4. 不做向量数据库和 RAG 检索。
5. 不把框架绑死在某一个模型供应商上。

## 2. Agent 框架的核心问题

一个 Agent 框架本质上是一个运行时系统。它需要处理：

```text
User Input
  -> Agent instructions
  -> Model call
  -> Tool call decision
  -> Tool execution
  -> Tool result returned to model
  -> Final answer
  -> Memory save
```

这套流程背后有几个稳定抽象：

| 抽象 | 解决的问题 | 当前实现 |
| --- | --- | --- |
| `Message` | 如何表示对话、工具调用和工具结果 | `types.py` |
| `ModelClient` | 如何隔离不同模型 API | `models.py` |
| `Tool` | 如何把 Python 函数暴露给模型 | `tools.py` |
| `Agent` | 如何组合模型、工具、指令和 memory | `agent.py` |
| `Memory` | 如何保存和恢复对话历史 | `memory.py` |
| `builtin_tools` | 如何提供安全的内置工具 | `builtin_tools.py` |

## 3. 主流框架如何设计

主流 Agent 框架的侧重点不同：

| 框架 | 设计重点 | 对我们的启发 |
| --- | --- | --- |
| OpenAI Agents SDK | Agent、Runner、Tools、Handoffs、Sessions、Tracing | 我们需要把 Agent、执行循环、工具、记忆和追踪分开 |
| LangGraph | 状态图、checkpoint、恢复执行、人类审批 | 后续 workflow 不应塞进 `Agent`，应该单独做 graph/runner |
| CrewAI | 多角色 Agent、Task、Crew、Process | 多 Agent 应该建立在单 Agent 稳定之后 |
| smolagents | 小核心、工具和 CodeAgent | 学习型框架要避免过早抽象 |
| Pydantic AI | 类型、结构化输出、工具参数校验 | 后续可以增强 schema 和输出验证 |

参考：

- OpenAI Agents SDK: https://openai.github.io/openai-agents-python/
- LangGraph: https://docs.langchain.com/oss/python/langgraph/
- CrewAI: https://docs.crewai.com/
- smolagents: https://huggingface.co/docs/smolagents/
- Pydantic AI: https://ai.pydantic.dev/

## 4. 当前架构

```text
agent_framework/
  agent.py         Agent 主循环
  models.py        模型适配层
  tools.py         工具注册和 schema 生成
  memory.py        memory 接口和实现
  types.py         Message、ToolCall、AgentResult
  builtin_tools.py 安全内置工具
  config.py        .env 加载
```

核心依赖方向：

```text
Agent
  depends on -> ModelClient
  depends on -> ToolRegistry
  depends on -> Memory
  depends on -> Message / ModelResponse
```

模型不知道工具怎么执行，工具不知道模型是谁，memory 不知道模型供应商是谁。这是刻意设计的。

## 5. Agent 主循环

当前 `Agent.run()` 做了这些事：

1. 组装 system instructions。
2. 从 memory 或显式 `history` 加载历史。
3. 追加当前用户输入。
4. 调用模型。
5. 如果模型返回最终回答，保存 memory 并结束。
6. 如果模型返回工具调用，执行工具。
7. 把工具结果作为 `tool` 消息追加回上下文。
8. 继续下一轮，直到有最终回答或达到 `max_steps`。

简化版伪代码：

```text
messages = [system] + memory.load() + [user]

for step in max_steps:
    response = model.complete(messages, tool_schemas)
    messages.append(assistant response)

    if no tool calls:
        memory.save(messages without system)
        return final answer

    for tool_call in response.tool_calls:
        result = run_tool(tool_call)
        messages.append(tool result)

memory.save(messages without system)
return stopped message
```

为什么这样做：

- Agent 主循环是最小可解释单元。
- 工具调用必须由框架执行，不能让模型直接执行代码。
- `max_steps` 是安全阀，避免模型无限工具调用。
- memory 保存时去掉 system message，避免重复保存固定指令。

## 6. ModelClient 设计

当前模型层有三类：

| 类 | 用途 |
| --- | --- |
| `RuleBasedDemoModel` | 不需要 API Key 的教学模型 |
| `OpenAICompatibleClient` | `/chat/completions` 风格接口 |
| `OpenAIResponsesClient` | `/responses` 风格接口，适配当前远程模型 |

为什么要有 `ModelClient`：

1. Agent 不应该关心底层 API 是 Responses 还是 Chat Completions。
2. 后续可以新增 Claude、Ollama、vLLM、本地模型。
3. 测试时可以用 fake model，不需要真实远程请求。

当前简化：

- 暂不支持 streaming。
- 暂不支持 multimodal input。
- 暂不支持 token usage 统计。
- 错误处理只做了基础包装。

后续演进：

```text
ModelClient
  -> complete()
  -> stream()
  -> count_tokens()
  -> capabilities()
```

## 7. Tool 设计

当前工具系统支持：

1. 用 `@tool` 把 Python 函数注册为工具。
2. 从函数签名生成 JSON schema。
3. 根据模型请求执行工具。
4. 把工具结果转成字符串回传给模型。

示例：

```python
@tool
def add(a: float, b: float) -> float:
    return a + b
```

为什么工具要独立抽象：

- 模型只负责决定“是否调用工具”和“传什么参数”。
- 框架负责实际执行工具。
- 工具是 Agent 能力边界，也是安全边界。

当前简化：

- 参数校验比较弱，只根据函数签名生成基础 schema。
- 没有工具级权限系统。
- 没有工具执行 tracing。
- 没有异步工具。

后续演进：

```text
Tool
  -> schema
  -> validate(args)
  -> run(args)
  -> permissions
  -> timeout
  -> trace
```

## 8. Memory 设计

当前 memory 是一个接口：

```text
load() -> List[Message]
save(messages)
clear()
```

已有实现：

| 实现 | 用途 |
| --- | --- |
| `InMemoryMemory` | 测试和临时对话 |
| `JsonFileMemory` | 本地持久化对话历史 |

为什么不直接把 history 写死在 Agent 里：

1. Agent 不应该关心历史存在哪里。
2. 测试、文件、数据库、向量库应该可以替换。
3. 后续多会话需要 `session_id`，不应该改 Agent 主逻辑。

主流框架里的 memory 通常分层：

| 层级 | 含义 | 当前是否实现 |
| --- | --- | --- |
| Short-term memory | 当前会话历史 | 已实现 |
| Session persistence | 多会话隔离和恢复 | 未实现 |
| Long-term memory | 用户偏好、长期事实 | 未实现 |
| Semantic memory | 向量检索、RAG | 未实现 |
| Task state | 工作流执行到哪一步 | 未实现 |

当前 memory 保存的是消息历史，不是“智能记忆”。它能回答“刚才说过什么”，但还不会自动提炼长期偏好。

下一步应该加：

```text
SessionMemory
  session_id
  list_sessions()
  delete_session()
```

## 9. Builtin File Tools 设计

当前内置文件工具：

| 工具 | 用途 |
| --- | --- |
| `list_files` | 列出项目文件 |
| `read_text_file` | 读取文本文件 |
| `search_text` | 搜索文本内容 |

关键安全设计：

1. 所有路径都限制在传入的 project root 下。
2. `../outside.txt` 这类路径会被拒绝。
3. 默认忽略 `.venv`、`.git`、`__pycache__` 等目录。
4. 文件读取有最大字符数。
5. 文本搜索跳过过大的文件。

为什么先做读文件工具：

- 它能让 Agent 理解自己的代码库。
- 风险比写文件、执行 shell 小。
- 对后续 RAG、代码助手、多 Agent 都有基础价值。

后续写文件和执行命令必须增加审批机制，不能直接暴露。

## 10. Trace 设计

Trace 是 Agent 运行时的观察层。它回答这些问题：

1. Agent 一次运行调用了几次模型？
2. 每次模型调用时上下文里有多少条消息？
3. 模型请求了哪些工具？
4. 工具执行成功还是失败？
5. memory 加载和保存了多少消息？
6. 运行结束原因是最终回答还是达到最大步数？

当前 trace 抽象：

```text
TraceEvent
  name
  timestamp
  data

Tracer
  record(event)
```

已有实现：

| 实现 | 用途 |
| --- | --- |
| `NoopTracer` | 默认 tracer，不记录事件 |
| `InMemoryTracer` | 测试和临时调试 |
| `JsonlTracer` | 写入 JSONL 文件，方便长期排查 |

为什么 Trace 不直接用 `print`：

- `print` 只是文本，后续很难过滤和统计。
- Trace 是结构化事件，可以写文件、进数据库或接 UI。
- Trace 可以控制隐私边界，只记录元数据，不记录完整 prompt。

当前记录的事件：

```text
agent.run.start
memory.load
model.call.start
model.call.end
model.call.error
tool.call.start
tool.call.end
tool.call.error
memory.save
agent.run.end
```

当前简化：

- 不记录完整 messages 内容。
- 不记录 token usage。
- 不记录模型响应原始 payload。
- 不做 trace id / parent id。
- 不做跨 Agent trace 关联。

后续演进：

```text
TraceEvent
  trace_id
  span_id
  parent_span_id
  duration_ms
  status
  metadata
```

这会让 trace 更接近 OpenTelemetry，也更适合多 Agent 和 workflow。

## 11. 配置设计

当前配置来自 `.env`：

```text
OPENAI_API_KEY
AGENT_MODEL
AGENT_BASE_URL
AGENT_WIRE_API
AGENT_REASONING_EFFORT
AGENT_RESPONSES_PATH
AGENT_TIMEOUT
```

为什么不用 Python 文件硬编码：

- API Key 不应该写进代码。
- 模型、base URL 和 timeout 经常需要切换。
- 示例和框架应该分开。

当前简化：

- `.env` 加载器只支持简单 `KEY=value`。
- 没有配置校验。
- 没有多 provider 配置文件。

后续可以增加：

```text
AgentConfig
ProviderConfig
load_config("agent.toml")
```

## 12. 当前设计取舍

### 为什么不用 LangGraph 起步

LangGraph 很适合复杂状态流，但新手一开始会被 graph、state、checkpoint、node、edge 分散注意力。我们先手写 Agent loop，是为了理解最小机制。

### 为什么不用 CrewAI 起步

CrewAI 适合多角色任务协作，但多 Agent 会放大调试难度。单 Agent 的模型调用、工具调用、memory 还没稳定前，不应该先引入多 Agent。

### 为什么不用完整 OpenAI Agents SDK

OpenAI Agents SDK 已经有成熟抽象，但我们当前目标是学习和自建框架。直接使用 SDK 会更快，但不利于理解 Agent runtime 的内部结构。

### 为什么当前 memory 只是消息历史

这是最容易验证、最不容易误导的 memory。长期记忆和语义记忆需要抽取、去重、检索、遗忘策略，过早做会让设计复杂化。

## 13. 设计原则

1. 接口稳定，实现可替换。
2. Agent 负责组合，不负责所有细节。
3. ModelClient 隔离供应商协议。
4. Tool 是能力边界，也是安全边界。
5. Memory 先做短期历史，再做长期记忆。
6. Workflow 不塞进 Agent，后续单独做 Runner 或 Graph。
7. 每增加一个危险能力，先设计权限和审计。
8. 测试优先覆盖运行时契约，而不是只测示例。

## 14. 后续路线

推荐演进顺序：

1. `SessionMemory`: 支持多会话和会话列表。
2. `Guardrails`: 工具权限、危险操作确认、路径和大小限制。
3. `Structured Output`: 用 schema 校验模型最终输出。
4. `RAG`: 文档切分、索引、检索、引用来源。
5. `Workflow`: 状态图、条件分支、人工审批。
6. `Multi-Agent`: 基于稳定 workflow 做角色分工。
7. `Eval`: 为 Agent 行为写可重复评测。

Trace 已经完成最小版本。下一步最建议做 `SessionMemory`，因为当前 `JsonFileMemory` 只有一个历史文件，不能自然区分多个会话。
