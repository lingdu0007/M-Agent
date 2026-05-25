# Hello Agent

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

第一版我们只实现最核心的四块：`Message`、`Tool`、`ModelClient`、`Agent`。

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
    trace_agent.py         # 展示 Agent 运行过程的 trace 事件
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

1. 增加 memory：把历史消息保存到文件或 SQLite。
2. 增加 RAG：让 Agent 能读取你的文档。
3. 增加 workflow：支持多个步骤和条件分支。
4. 增加 multi-agent：让不同 Agent 分工协作。
5. 增加 tracing：记录每次模型调用、工具调用和耗时。
6. 增加 guardrails：限制危险工具、校验输入输出。

建议你先把第一版代码读懂，再加一个自己的工具，例如天气查询、文件搜索、网页抓取或数据库查询。
