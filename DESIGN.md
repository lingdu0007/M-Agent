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

1. 不做复杂自由对话式多 Agent 编排。
2. 不做可视化工作流。
3. 不做生产级权限系统。
4. 不做生产级向量数据库和高级 RAG 检索。
5. 不把框架绑死在某一个模型供应商上。
6. 不在第一版 eval 里引入不可复现的 judge 模型评分。

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
| `SessionMemory` | 如何隔离多个会话的历史 | `session_memory.py` |
| `Guardrail` | 如何在工具执行前做安全策略检查 | `guardrails.py` |
| `OutputSchema` | 如何约束最终回答的数据结构 | `structured_output.py` |
| `RAG` | 如何检索知识片段并交给 Agent | `rag.py` |
| `Workflow` | 如何组织多步骤任务 | `workflow.py` |
| `MultiAgentTeam` | 如何让多个 Agent 受控协作 | `multi_agent.py` |
| `Eval` | 如何衡量 Agent 输出质量并做回归检查 | `evals.py` |
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
  session_memory.py 多会话 memory
  guardrails.py    工具调用安全策略
  structured_output.py 结构化输出解析和校验
  rag.py           本地文档切块、索引和检索
  workflow.py      多步骤任务编排
  multi_agent.py   受控多 Agent 协作
  evals.py         确定性评测和报告
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
| Session persistence | 多会话隔离和恢复 | 已实现基础版 |
| Long-term memory | 用户偏好、长期事实 | 未实现 |
| Semantic memory | 向量检索、RAG | 基础 RAG 已实现，向量检索未实现 |
| Task state | 工作流执行到哪一步 | 未实现 |

当前 memory 保存的是消息历史，不是“智能记忆”。它能回答“刚才说过什么”，但还不会自动提炼长期偏好。

多会话 memory 已经作为独立接口实现：

```text
SessionMemory
  load(session_id)
  save(session_id, messages)
  clear(session_id)
  list_sessions()
  delete_session()
```

已有实现：

| 实现 | 用途 |
| --- | --- |
| `InMemorySessionMemory` | 测试和临时多会话 |
| `JsonDirectorySessionMemory` | 一个 session 一个 JSON 文件 |

为什么 `SessionMemory` 不直接合并进 `Memory`：

- 单会话 memory 和多会话 memory 的职责不同。
- `Memory` 只回答“当前历史是什么”。
- `SessionMemory` 还要回答“有哪些会话、删除哪个会话、当前请求属于哪个会话”。
- 分开后，Agent 可以兼容简单场景和多用户场景。

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
session.load
model.call.start
model.call.end
model.call.error
tool.call.start
tool.call.end
tool.call.error
memory.save
session.save
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

## 11. Guardrails 设计

Guardrails 是 Agent 的安全策略层。它不负责执行工具，也不负责模型推理，只负责回答一个问题：

```text
这个工具调用是否允许执行？
```

当前抽象：

```text
Guardrail
  check_tool_call(tool_name, arguments) -> GuardrailDecision

GuardrailDecision
  allowed
  reason
  rule
```

已有规则：

| 规则 | 用途 |
| --- | --- |
| `ToolAllowlistGuardrail` | 只允许指定工具 |
| `ToolDenylistGuardrail` | 禁止指定工具 |
| `SensitiveArgumentGuardrail` | 阻止敏感参数 key 或 value |

为什么放在工具执行前：

- 工具是 Agent 能力边界，也是风险边界。
- 模型可能请求不应该执行的工具。
- 参数里可能包含 secret、token、私密路径或危险命令。
- 被阻止的结果仍会作为 tool message 回给模型，模型可以解释为什么不能完成。

当前简化：

- 只有同步检查，没有人工审批。
- 只检查工具名和参数，不检查最终回答。
- 不支持按用户、项目、环境区分权限。
- 不支持风险分级。

后续演进：

```text
Guardrail
  check_input(prompt)
  check_tool_call(tool_name, arguments)
  check_tool_result(tool_name, result)
  check_output(answer)

Decision
  allow
  block
  require_approval
```

## 12. Structured Output 设计

Structured Output 解决的问题是：Agent 的最终回答如果要被程序继续处理，不能只是一段自然语言。

当前抽象：

```text
OutputSchema
  name
  description
  schema

AgentResult
  output
  structured_output
```

当前流程：

```text
Agent.run(prompt, output_schema)
  -> 把 schema 追加到 system instructions
  -> 模型返回最终回答
  -> 框架解析 JSON
  -> 框架按 schema 校验
  -> result.structured_output = parsed object
```

为什么先做框架级解析，而不是直接依赖模型供应商原生 structured output：

- 当前框架要保持模型适配层可替换。
- 有些 OpenAI-compatible 服务不完全支持原生 structured output。
- 教学阶段更容易看清楚“声明 schema、模型输出、框架校验”的关系。

主流框架通常有两种做法：

| 做法 | 优点 | 缺点 |
| --- | --- | --- |
| 模型原生 structured output | 稳定性更高，模型端约束更强 | 依赖具体供应商能力 |
| 框架解析和校验 | 可移植，适合多模型 | 模型仍可能输出无效 JSON |

当前简化：

- 只支持 JSON。
- 只实现 JSON schema 的基础类型、required、properties、items、enum。
- 校验失败直接抛 `StructuredOutputError`。
- 没有自动重试修复。

后续演进：

```text
StructuredOutput
  -> provider-native schema
  -> validation retry
  -> pydantic/dataclass schema generation
  -> partial parsing
  -> typed AgentResult[T]
```

## 13. RAG 设计

RAG 是 Retrieval-Augmented Generation。它解决的问题是：模型本身不知道你的项目文档、代码说明或私有知识库，需要先检索相关片段，再基于片段回答。

主流 RAG 通常分成几层：

```text
Document Loader
  -> Chunker
  -> Embedding / Index
  -> Retriever
  -> Context Builder
  -> Generator
```

当前抽象：

```text
DocumentChunk
  id
  source
  text
  start_line
  end_line

KeywordRagIndex
  add_text()
  from_directory()
  search()
  get()

RAG tools
  search_knowledge(query, top_k)
  read_knowledge_chunk(chunk_id)
```

为什么先做关键词检索，而不是直接做向量数据库：

- 关键词检索无依赖，便于理解 RAG 的模块边界。
- 当前目标是先把 loader、chunk、retriever、tool 这条链路跑通。
- 后续换成 embedding/vector store 时，不应该改 Agent 主循环。

当前简化：

- 不使用 embedding。
- 不做 rerank。
- 不做引用去重。
- 不做增量索引。
- 只索引常见文本文件后缀。

安全边界：

- 默认忽略 `.env`。
- 默认忽略 `data/`，避免索引 memory 和 session 历史。
- 默认忽略 `.venv`、`.git`、缓存目录和构建目录。
- 默认跳过过大文件。

后续演进：

```text
RAG
  -> embedding retriever
  -> vector store
  -> reranker
  -> citation builder
  -> incremental index
  -> hybrid search
```

## 14. Workflow 设计

Workflow 是 Agent 之上的编排层。Agent 负责一次推理循环，Workflow 负责组织多个步骤完成一个任务。

当前抽象：

```text
WorkflowContext = dict

StepResult
  updates
  next_step
  stop

FunctionStep
  run(context)

AgentStep
  agent.run(prompt(context))

Workflow
  start
  steps
  max_steps
```

为什么 Workflow 不放进 Agent：

- Agent 的职责是推理和工具调用。
- Workflow 的职责是任务编排和状态流转。
- 混在一起会让 Agent 既像模型包装器，又像流程引擎，边界会变差。

这对应 LangGraph 这类框架里的状态图思想，但当前版本只保留最小能力：

- 顺序执行。
- 条件跳转。
- 提前结束。
- 防无限循环。
- trace 事件。

当前简化：

- 没有并行步骤。
- 没有持久化 checkpoint。
- 没有人工审批节点。
- 没有可视化图。
- 没有失败重试策略。

后续演进：

```text
Workflow
  -> checkpoint
  -> retry policy
  -> human approval step
  -> parallel branches
  -> graph visualization
```

## 15. Multi-Agent 设计

Multi-Agent 解决的问题是：复杂任务通常需要不同角色分工，而不是让一个 Agent 同时承担研究、审查、写作和决策。

主流框架里常见几种做法：

| 模式 | 代表 | 特点 |
| --- | --- | --- |
| 顺序角色协作 | CrewAI 风格 | 研究员、审阅者、写作者按任务顺序协作 |
| 自由对话 | AutoGen 风格 | 多个 Agent 互相发消息，灵活但更难控制 |
| Handoff | OpenAI Agents SDK 风格 | 一个 Agent 决定把任务交给另一个 Agent |

当前实现选择第一种：受控顺序团队。

当前抽象：

```text
TeamMember
  name
  role
  agent
  prompt(context)
  output_key

MultiAgentTeam
  members
  run(task, context)

MultiAgentResult
  task
  final_output
  context
  member_outputs
  agent_results
```

为什么不先做自由对话：

- 自由多 Agent 容易无限循环。
- 角色之间的消息边界更难调试。
- 新手阶段更需要可预测、可测试的执行顺序。
- Workflow 已经提供了顺序编排的基础，顺序团队更自然。

当前简化：

- 不支持 Agent 自主选择 handoff。
- 不支持多轮 Agent 间辩论。
- 不支持并行角色执行。
- 不支持投票或裁判 Agent。

后续演进：

```text
MultiAgent
  -> handoff rules
  -> debate loop
  -> judge / critic agent
  -> parallel team branches
  -> role-specific memory
```

## 16. Eval 设计

Eval 解决的问题是：Agent 看起来能回答，不代表它在稳定变好。每次改模型、prompt、工具、memory 或 workflow，都可能让旧任务悄悄退化。Eval 提供一组可重复运行的样例和判断规则，用来做质量测量和回归保护。

主流 Agent 框架里的 eval 通常有几种层次：

| 类型 | 适合检查什么 | 风险 |
| --- | --- | --- |
| 确定性断言 | 精确答案、关键词、结构化字段 | 覆盖不了主观质量 |
| LLM-as-judge | 写作质量、完整性、推理过程 | 成本更高，结果可能波动 |
| 轨迹评测 | 是否调用正确工具、调用顺序是否合理 | 需要稳定 trace/tool-call 数据 |
| 人工评审 | 高风险、强主观任务 | 慢，不适合每次回归 |

当前实现选择确定性断言。

当前抽象：

```text
EvalCase
  name
  prompt
  expected_output
  expected_keywords
  expected_structured
  expected_tool_calls
  output_schema

Evaluator
  evaluate(case, result) -> EvalCheck

EvalRunner
  target(case)
  evaluators
  run(cases) -> EvalReport
```

已有 evaluator：

| Evaluator | 用途 |
| --- | --- |
| `ExactMatchEvaluator` | 最终输出必须和期望文本完全一致 |
| `ContainsKeywordsEvaluator` | 最终输出必须包含指定关键词 |
| `StructuredFieldEvaluator` | `structured_output` 中指定字段必须匹配 |
| `ToolTrajectoryEvaluator` | 工具调用数量、顺序和参数子集必须匹配 |

Tool trajectory eval 检查的是模型请求过哪些工具，而不是工具最终返回了什么。当前实现直接读取 `AgentResult.messages` 里的 assistant `tool_calls`，这是 Agent 运行结果里最直接的事实来源。

为什么 Eval 不放进 `Agent`：

- Agent 的职责是执行推理循环。
- Eval 的职责是测量质量。
- 同一个 Agent 可以被不同 eval suite 测试。
- Workflow、Multi-Agent 也应该能被评测，而不是只有单 Agent 能评测。

当前简化：

- 没有 LLM-as-judge。
- 工具轨迹只检查工具名和参数子集，不检查工具返回值质量。
- 工具轨迹来自 `AgentResult.messages`，还没有做 trace/span 级别断言。
- 没有 HTML 报告。
- 没有数据集文件加载。
- 没有统计置信区间。

后续演进：

```text
Eval
  -> dataset loader
  -> tool result evaluator
  -> trace/span trajectory evaluator
  -> LLM judge evaluator
  -> JSON/HTML report
  -> CI regression gate
```

## 17. 配置设计

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

## 18. 当前设计取舍

### 为什么不用 LangGraph 起步

LangGraph 很适合复杂状态流，但新手一开始会被 graph、state、checkpoint、node、edge 分散注意力。我们先手写 Agent loop，是为了理解最小机制。

### 为什么不用 CrewAI 起步

CrewAI 适合多角色任务协作，但多 Agent 会放大调试难度。单 Agent 的模型调用、工具调用、memory 还没稳定前，不应该先引入多 Agent。

### 为什么不用完整 OpenAI Agents SDK

OpenAI Agents SDK 已经有成熟抽象，但我们当前目标是学习和自建框架。直接使用 SDK 会更快，但不利于理解 Agent runtime 的内部结构。

### 为什么当前 memory 只是消息历史

这是最容易验证、最不容易误导的 memory。长期记忆和语义记忆需要抽取、去重、检索、遗忘策略，过早做会让设计复杂化。

## 19. 设计原则

1. 接口稳定，实现可替换。
2. Agent 负责组合，不负责所有细节。
3. ModelClient 隔离供应商协议。
4. Tool 是能力边界，也是安全边界。
5. Memory 先做短期历史，再做长期记忆。
6. Workflow 不塞进 Agent，后续单独做 Runner 或 Graph。
7. 每增加一个危险能力，先设计权限和审计。
8. 测试优先覆盖运行时契约，而不是只测示例。

## 20. 后续路线

推荐演进顺序：

1. `Eval`: 增加工具结果评测、trace/span 轨迹评测、LLM-as-judge 和报告导出。
2. `RAG`: 加入 embedding、vector store、rerank 和 citation。
3. `Workflow`: 增加 checkpoint、retry、human approval 和并行分支。
4. `Trace`: 增加 trace id、span id、duration、token usage。
5. `Guardrails`: 从工具调用前扩展到输入、工具结果和最终输出。

Trace、SessionMemory、工具调用前 Guardrails、Structured Output、基础 RAG、Workflow、Multi-Agent 和确定性 Eval 都已经完成最小版本。后续重点应该从“能跑”转向“跑得稳、可恢复、可审计、可比较”。
