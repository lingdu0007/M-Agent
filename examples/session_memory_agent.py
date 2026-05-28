import os
from pathlib import Path

from agent_framework import (
    Agent,
    JsonDirectorySessionMemory,
    OpenAIResponsesClient,
    load_env_file,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_env_file(PROJECT_ROOT / ".env")

WIRE_API = os.getenv("AGENT_WIRE_API", "responses")
if WIRE_API != "responses":
    raise RuntimeError(
        f"examples/session_memory_agent.py expects AGENT_WIRE_API=responses, got {WIRE_API}"
    )

model = OpenAIResponsesClient(
    model=os.getenv("AGENT_MODEL", "gpt-5.5"),
    base_url=os.getenv("AGENT_BASE_URL", "https://codex.ciii.club"),
    reasoning_effort=os.getenv("AGENT_REASONING_EFFORT", "xhigh"),
    endpoint_path=os.getenv("AGENT_RESPONSES_PATH", "/responses"),
    timeout=int(os.getenv("AGENT_TIMEOUT", "120")),
)

agent = Agent(
    name="SessionMemoryAgent",
    instructions="You are a helpful assistant. Use session history when it is relevant.",
    model=model,
    session_memory=JsonDirectorySessionMemory(PROJECT_ROOT / "data" / "sessions"),
)


if __name__ == "__main__":
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is empty. Fill it in .env or export it before running.")

    first = agent.run(
        "请记住：这个会话 alpha 里，我正在开发一个自己的 Python Agent 框架。",
        session_id="alpha",
    )
    print("Alpha first:", first.output)

    alpha_second = agent.run("我刚才说我在开发什么？", session_id="alpha")
    print("Alpha second:", alpha_second.output)

    beta_first = agent.run("我刚才说我在开发什么？", session_id="beta")
    print("Beta first:", beta_first.output)

    print("Sessions:", agent.list_sessions())
