import os
from pathlib import Path

from agent_framework import Agent, OpenAIResponsesClient, load_env_file, make_file_tools


PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_env_file(PROJECT_ROOT / ".env")

WIRE_API = os.getenv("AGENT_WIRE_API", "responses")
if WIRE_API != "responses":
    raise RuntimeError(f"examples/file_agent.py expects AGENT_WIRE_API=responses, got {WIRE_API}")

model = OpenAIResponsesClient(
    model=os.getenv("AGENT_MODEL", "gpt-5.5"),
    base_url=os.getenv("AGENT_BASE_URL", "https://codex.ciii.club"),
    reasoning_effort=os.getenv("AGENT_REASONING_EFFORT", "xhigh"),
    endpoint_path=os.getenv("AGENT_RESPONSES_PATH", "/responses"),
    timeout=int(os.getenv("AGENT_TIMEOUT", "120")),
)

agent = Agent(
    name="FileAgent",
    instructions=(
        "You are a local project assistant. Use file tools to inspect files under "
        "the project root before answering questions about the codebase."
    ),
    model=model,
    tools=make_file_tools(PROJECT_ROOT),
)


if __name__ == "__main__":
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is empty. Fill it in .env or export it before running.")

    result = agent.run(
        "请先列出 src/agent_framework 下的文件，"
        "再阅读 src/agent_framework/agent.py，"
        "最后用三句话总结 Agent 主循环做了什么。"
    )
    print(result.output)
