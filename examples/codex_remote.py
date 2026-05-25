import os
from pathlib import Path

from agent_framework import Agent, OpenAIResponsesClient, load_env_file, tool


load_env_file(Path(__file__).resolve().parents[1] / ".env")

WIRE_API = os.getenv("AGENT_WIRE_API", "responses")
if WIRE_API != "responses":
    raise RuntimeError(f"examples/codex_remote.py expects AGENT_WIRE_API=responses, got {WIRE_API}")


@tool
def add(a: float, b: float) -> float:
    """Add two numbers."""
    return a + b


model = OpenAIResponsesClient(
    model=os.getenv("AGENT_MODEL", "gpt-5.5"),
    base_url=os.getenv("AGENT_BASE_URL", "https://codex.ciii.club"),
    reasoning_effort=os.getenv("AGENT_REASONING_EFFORT", "xhigh"),
    endpoint_path=os.getenv("AGENT_RESPONSES_PATH", "/responses"),
    timeout=int(os.getenv("AGENT_TIMEOUT", "120")),
)

agent = Agent(
    name="CodexRemoteAgent",
    instructions="You are a concise assistant. Use tools when they are useful.",
    model=model,
    tools=[add],
)


if __name__ == "__main__":
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is empty. Fill it in .env or export it before running.")
    result = agent.run("请用工具计算 21 + 21，然后用一句中文回答。")
    print(result.output)
