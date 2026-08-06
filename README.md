# M-Agent

M-Agent is an embeddable Agent Application Runtime for Python. Its primary
public distribution is `m-agent`, and its public runtime package is
`m_agent`. Runtime Integrators should start with
[the Durable Run guide](docs/durable-run.md): the async-first `Runner`
executes explicitly supplied Agent Runs, while applications retain ownership
of workers, queues, polling, scheduling, and lease takeover.

The legacy learning framework remains in `agent_framework` with the existing
examples below. It is retained as existing repository behavior, not as the
primary M-Agent runtime interface; this Ticket does not introduce a migration
or compatibility policy for it.

## Legacy Learning Framework Reference

这是一个给新手学习用的最小 Agent 框架。目标不是复制大型框架，而是先把 Agent 的核心机制跑通：

1. Agent 接收用户任务。
2. Model 决定直接回答，还是调用工具。
3. Agent 执行工具，把结果放回消息列表。
4. Model 基于工具结果给出最终回答。

架构设计说明见 [DESIGN.md](DESIGN.md)。

## 我参考的 GitHub 项目

- OpenAI Agents SDK: 轻量、多 Agent、工具、handoff、session、tracing。
- LangGraph: 低层状态图编排，适合长流程、有状态 Agent。
- CrewAI: 面向多角色 Agent 协作和任务编排。
- smolagents: 代码量克制，强调 CodeAgent 和工具执行。
- Pydantic AI: 强调类型、结构化输出和生产级 Python 体验。
- Microsoft Agent Framework: 面向生产部署、多语言、多 Agent 工作流。
- Google ADK: code-first 的 Agent 构建、评估、部署工具包。
- AutoGen: 经典多 Agent 项目，但当前更适合作为历史参考。

第一阶段从最核心的四块开始：`Message`、`Tool`、`ModelClient`、`Agent`。现在已经在这个核心上继续扩展了 memory、trace、guardrails、RAG、workflow、multi-agent 和 eval。

## 项目结构

```text
hello-agent/
  src/agent_framework/
    agent.py      # Agent 主循环
    models.py     # 模型适配层
    tools.py      # 工具注册和 schema 生成
    types.py      # 消息、工具调用、运行结果
  examples/
    hello_offline.py       # 不需要 API Key 的本地演示
    openai_compatible.py   # 接入 OpenAI-compatible 模型
    codex_remote.py        # 使用你的 Codex Responses 配置
    file_agent.py          # 能读取本项目文件的远程 Agent
    memory_agent.py        # 带 JSON 持久化 memory 的远程 Agent
    session_memory_agent.py # 多会话 memory 示例
    trace_agent.py         # 展示 Agent 运行过程的 trace 事件
    guardrails_agent.py    # 展示工具调用前的安全拦截
    structured_output_agent.py # 展示结构化输出解析和校验
    rag_agent.py           # 展示本地文档检索增强生成
    workflow_agent.py      # 展示多步骤任务编排
    multi_agent_team.py    # 展示受控多 Agent 协作
    eval_agent.py          # 展示确定性评测和汇总报告
  tests/
    test_agent.py
```

## 第一步：跑通离线示例

```bash
cd /Users/lingdu/workspace/agent/hello-agent
uv run --with-editable . python examples/hello_offline.py
```

预期你会看到类似输出：

```text
add returned: 8.0
get_current_time returned: 2026-05-25 11:30:00
```

这里的 `RuleBasedDemoModel` 不是真正的大模型，它只是模拟“大模型决定调用工具”的过程，方便你理解 Agent 循环。

## 第二步：运行测试

```bash
cd /Users/lingdu/workspace/agent/hello-agent
uv run --with-editable . python -m unittest discover -s tests
```

## 第三步：接入真实模型

本框架内置了一个最小的 OpenAI-compatible 客户端。OpenAI、DeepSeek、通义千问的 OpenAI-compatible endpoint、OpenRouter、本地 vLLM/Ollama 代理等，都可以用类似方式接入。

```bash
cd /Users/lingdu/workspace/agent/hello-agent
export OPENAI_API_KEY="你的 API Key"
export OPENAI_MODEL="你的模型名"
export OPENAI_BASE_URL="https://api.openai.com/v1"
uv run --with-editable . python examples/openai_compatible.py
```

如果使用其他供应商，把 `OPENAI_BASE_URL` 换成对应的 OpenAI-compatible 地址即可。

## 使用你的 Codex 远程模型配置

你提供的 Codex 配置里，真正需要进入本框架的是这些字段：

```text
model = "gpt-5.5"
base_url = "https://codex.ciii.club"
wire_api = "responses"
model_reasoning_effort = "xhigh"
requires_openai_auth = true
```

对应到本项目：

```text
AGENT_MODEL=gpt-5.5
AGENT_BASE_URL=https://codex.ciii.club
AGENT_WIRE_API=responses
AGENT_REASONING_EFFORT=xhigh
AGENT_RESPONSES_PATH=/responses
OPENAI_API_KEY=你的认证密钥
```

`review_model` 是 Codex 做代码审查时用的模型，本框架暂时不需要。`network_access` 和 `windows_wsl_setup_acknowledged` 是 Codex 运行环境配置，也不属于 Agent 框架配置。

运行示例：

```bash
cd /Users/lingdu/workspace/agent/hello-agent
export OPENAI_API_KEY="你的认证密钥"
uv run --with-editable . python examples/codex_remote.py
```

`examples/codex_remote.py` 会自动读取项目根目录的 `.env`。如果你已经把 `OPENAI_API_KEY` 写进 `.env`，就不需要再手动 `export`；但这个值不能为空。

## 第四步：让 Agent 读取项目文件

本项目提供了一组受限文件工具：`list_files`、`read_text_file`、`search_text`。这些工具只能访问你传入的项目根目录，不能跳到系统其他目录。

```bash
cd /Users/lingdu/workspace/agent/hello-agent
uv run --with-editable . python examples/file_agent.py
```

这个示例会让远程模型先列出 `src/agent_framework` 下的文件，再读取 `agent.py`，最后总结 Agent 主循环。

## 第五步：增加 Memory

框架现在有两种 memory：

- `InMemoryMemory`: 进程内保存，适合测试和临时对话。
- `JsonFileMemory`: 保存到 JSON 文件，重启程序后仍能读取历史。

运行持久化 memory 示例：

```bash
cd /Users/lingdu/workspace/agent/hello-agent
uv run --with-editable . python examples/memory_agent.py
```

示例会把对话历史保存到 `data/memory.json`。这个目录已经加入 `.gitignore`，不会误提交你的对话内容。

在你自己的 Agent 里使用：

```python
from pathlib import Path
from agent_framework import Agent, JsonFileMemory, OpenAIResponsesClient

agent = Agent(
    name="MyAgent",
    instructions="Use conversation history when relevant.",
    model=OpenAIResponsesClient(),
    memory=JsonFileMemory(Path("data/memory.json")),
)
```

## 第六步：增加 Trace

Trace 用来观察 Agent 每一步做了什么。它和普通 `print` 不一样：Trace 是结构化事件，可以保存、过滤、统计，也可以后续接可视化面板。

当前支持：

- `InMemoryTracer`: 把事件保存在内存里，适合测试和调试。
- `JsonlTracer`: 把事件写入 JSONL 文件，适合长期排查。

离线运行 trace 示例：

```bash
cd /Users/lingdu/workspace/agent/hello-agent
uv run --with-editable . python examples/trace_agent.py
```

它会输出模型最终回答，以及类似这些事件：

```text
agent.run.start
memory.load
model.call.start
model.call.end
tool.call.start
tool.call.end
memory.save
agent.run.end
```

## 第七步：增加 SessionMemory

普通 `JsonFileMemory` 只有一个历史文件。`SessionMemory` 用 `session_id` 区分多段会话，适合一个 Agent 同时服务多个用户、多个任务或多个项目。

当前支持：

- `InMemorySessionMemory`: 进程内多会话，适合测试。
- `JsonDirectorySessionMemory`: 一个 session 一个 JSON 文件，适合本地持久化。

运行示例：

```bash
cd /Users/lingdu/workspace/agent/hello-agent
uv run --with-editable . python examples/session_memory_agent.py
```

在代码里使用：

```python
from agent_framework import Agent, JsonDirectorySessionMemory

agent = Agent(
    name="MyAgent",
    instructions="Use session history when relevant.",
    model=model,
    session_memory=JsonDirectorySessionMemory("data/sessions"),
)

agent.run("记住我正在开发 Agent 框架", session_id="project-a")
agent.run("我刚才说我在开发什么？", session_id="project-a")
```

## 第八步：增加 Guardrails

Guardrails 是工具调用前的安全策略。模型可以请求工具，但框架会先检查是否允许执行。

当前支持：

- `ToolAllowlistGuardrail`: 只允许指定工具。
- `ToolDenylistGuardrail`: 禁止指定工具。
- `SensitiveArgumentGuardrail`: 阻止敏感参数 key 或 value。

离线运行示例：

```bash
cd /Users/lingdu/workspace/agent/hello-agent
uv run --with-editable . python examples/guardrails_agent.py
```

在代码里使用：

```python
from agent_framework import Agent, ToolAllowlistGuardrail

agent = Agent(
    name="SafeAgent",
    instructions="Use tools only when allowed.",
    model=model,
    tools=[read_text_file],
    guardrails=[ToolAllowlistGuardrail(["read_text_file"])],
)
```

## 第九步：增加 Structured Output

Structured Output 用来让 Agent 的最终回答变成程序可消费的数据，而不是只靠自然语言。

当前做法：

1. 用 `OutputSchema` 描述期望 JSON。
2. Agent 把 schema 追加到 system instructions。
3. 模型最终回答后，框架解析 JSON。
4. 框架按 schema 做基础校验。
5. 校验后的对象放在 `result.structured_output`。

离线运行示例：

```bash
cd /Users/lingdu/workspace/agent/hello-agent
uv run --with-editable . python examples/structured_output_agent.py
```

在代码里使用：

```python
from agent_framework import OutputSchema

schema = OutputSchema(
    name="answer",
    schema={
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "score": {"type": "integer"},
        },
        "required": ["answer", "score"],
    },
)

result = agent.run("给我结构化答案", output_schema=schema)
print(result.structured_output["answer"])
```

## 第十步：增加 RAG

RAG 是 Retrieval-Augmented Generation：先从知识库检索相关片段，再让 Agent 基于片段回答。

当前实现是无依赖关键词检索，适合理解 RAG 边界：

- `KeywordRagIndex`: 本地关键词索引。
- `DocumentChunk`: 文档切块。
- `RetrievedChunk`: 检索结果。
- `make_rag_tools`: 把检索能力暴露成 Agent 工具。

离线运行示例：

```bash
cd /Users/lingdu/workspace/agent/hello-agent
uv run --with-editable . python examples/rag_agent.py
```

在代码里使用：

```python
from agent_framework import KeywordRagIndex, make_rag_tools

index = KeywordRagIndex.from_directory("/path/to/docs")
agent = Agent(
    name="RagAgent",
    instructions="Search knowledge before answering.",
    model=model,
    tools=make_rag_tools(index),
)
```

## 第十一步：增加 Workflow

Workflow 是 Agent 之上的编排层。Agent 负责一次推理循环，Workflow 负责把多个步骤组织成一个任务。

当前支持：

- `FunctionStep`: 普通 Python 函数步骤。
- `AgentStep`: 调用一个 Agent 的步骤。
- `StepResult.next(...)`: 指定下一步。
- `StepResult.done(...)`: 提前结束。
- `Workflow(..., max_steps=...)`: 防止无限循环。

离线运行示例：

```bash
cd /Users/lingdu/workspace/agent/hello-agent
uv run --with-editable . python examples/workflow_agent.py
```

在代码里使用：

```python
from agent_framework import Workflow, FunctionStep, AgentStep

workflow = Workflow(
    [
        FunctionStep("prepare", lambda ctx: {"topic": "Agent"}),
        AgentStep("draft", agent, lambda ctx: f"Explain {ctx['topic']}"),
        FunctionStep("finalize", lambda ctx: {"final": ctx["draft"]}),
    ],
    start="prepare",
)

result = workflow.run()
```

## 第十二步：增加 Multi-Agent

Multi-Agent 是多个 Agent 的角色化协作。当前实现是受控顺序团队，而不是开放式群聊。

当前支持：

- `TeamMember`: 一个角色、一个 Agent、一个 prompt builder。
- `MultiAgentTeam`: 按顺序运行多个成员。
- `MultiAgentResult`: 保存最终输出、上下文、每个成员输出和 AgentResult。

离线运行示例：

```bash
cd /Users/lingdu/workspace/agent/hello-agent
uv run --with-editable . python examples/multi_agent_team.py
```

在代码里使用：

```python
from agent_framework import MultiAgentTeam, TeamMember

team = MultiAgentTeam(
    [
        TeamMember("researcher", "Research", research_agent, lambda ctx: ctx["task"]),
        TeamMember("writer", "Write", writer_agent, lambda ctx: ctx["researcher"]),
    ],
)

result = team.run("Explain Agent memory")
print(result.final_output)
```

## 第十三步：增加 Eval

Eval 是质量测量层。Agent 负责生成结果，Eval 负责判断结果是否符合预期。

主流 Agent 项目里常见几种评测方式：

- 确定性断言：精确匹配、关键词包含、结构化字段匹配。
- LLM-as-judge：让另一个模型按 rubric 打分。
- 工具轨迹评测：检查 Agent 是否调用了正确工具、顺序是否合理。
- 人工评审：用于高风险或主观任务。

当前版本先做确定性评测，因为它可复现、便宜、适合当回归测试。

当前支持：

- `EvalCase`: 一条评测样例。
- `ExpectedToolCall`: 一条期望工具调用。
- `ExactMatchEvaluator`: 精确匹配最终输出。
- `ContainsKeywordsEvaluator`: 检查输出是否包含关键字。
- `StructuredFieldEvaluator`: 检查结构化输出里的字段值。
- `ToolTrajectoryEvaluator`: 检查工具调用顺序和参数子集。
- `EvalRunner`: 运行一组 case 并生成 `EvalReport`。
- `run_agent_evals`: 评测 Agent 的便捷函数。

离线运行示例：

```bash
cd /Users/lingdu/workspace/agent/hello-agent
uv run --with-editable . python examples/eval_agent.py
```

在代码里使用：

```python
from agent_framework import Agent, EvalCase, ExpectedToolCall, run_agent_evals

cases = [
    EvalCase(
        name="memory-answer",
        prompt="Explain Agent memory.",
        expected_keywords=["history", "session"],
    ),
    EvalCase(
        name="calculator-tool",
        prompt="Calculate 3 + 5.",
        expected_output="answer=8",
        expected_tool_calls=[ExpectedToolCall("add", {"a": 3, "b": 5})],
    )
]

report = run_agent_evals(agent, cases)
print(report.summary())
```

`ToolTrajectoryEvaluator` 默认严格检查工具调用数量和顺序。参数采用“期望子集”匹配：如果你只关心 `{"a": 3}`，模型多传了 `{"b": 5}` 也不会失败；如果你写了 `{"a": 3, "b": 5}`，两个值都必须匹配。

## 怎么开发你自己的 Agent

先写工具：

```python
from agent_framework import tool

@tool
def add(a: float, b: float) -> float:
    """Add two numbers."""
    return a + b
```

再组装 Agent：

```python
from agent_framework import Agent, OpenAICompatibleClient

agent = Agent(
    name="MyAgent",
    instructions="You are a helpful assistant. Use tools when useful.",
    model=OpenAICompatibleClient(),
    tools=[add],
)

result = agent.run("计算 12 + 30")
print(result.output)
```

## 下一阶段路线

1. 增强 Eval：增加 LLM-as-judge、工具轨迹评测和 HTML/JSON 报告。
2. 增强 RAG：加入 embedding、vector store、rerank 和引用生成。
3. 增强 Workflow：支持 checkpoint、重试、人工审批和并行分支。
4. 增强 Multi-Agent：支持 handoff、critic/judge Agent 和角色级 memory。
5. 增强 Trace：增加 trace id、span id、耗时和 token usage。
6. 增强 Guardrails：检查输入、工具结果和最终输出。

建议你先把第一版代码读懂，再加一个自己的工具，例如天气查询、文件搜索、网页抓取或数据库查询。
