import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_framework import (
    Agent,
    InMemoryMemory,
    InMemoryTracer,
    JsonFileMemory,
    JsonlTracer,
    Message,
    ModelResponse,
    TraceEvent,
    ToolCall,
    load_env_file,
    make_file_tools,
    tool,
)
from agent_framework.models import (
    _messages_to_responses,
    _response_to_model_response,
    _tool_to_responses_schema,
)


@tool
def add(a: float, b: float) -> float:
    return a + b


class AddThenAnswerModel:
    def complete(self, messages, tools):
        if messages[-1].role == "tool":
            return ModelResponse(content=f"answer={messages[-1].content}")
        return ModelResponse(
            tool_calls=[ToolCall(id="call_1", name="add", arguments={"a": 2, "b": 4})]
        )


class CountingModel:
    def complete(self, messages, tools):
        user_count = len([message for message in messages if message.role == "user"])
        return ModelResponse(content=f"user_count={user_count}")


class AgentFrameworkTests(unittest.TestCase):
    def test_tool_schema_and_run(self):
        schema = add.to_openai_schema()

        self.assertEqual(schema["function"]["name"], "add")
        self.assertEqual(schema["function"]["parameters"]["required"], ["a", "b"])
        self.assertEqual(add.run({"a": 1, "b": 2}), 3)

    def test_agent_runs_tool_then_returns_answer(self):
        agent = Agent(
            name="TestAgent",
            instructions="Use tools.",
            model=AddThenAnswerModel(),
            tools=[add],
        )

        result = agent.run("2 + 4")

        self.assertEqual(result.output, "answer=6")
        self.assertEqual(result.steps, 2)
        self.assertEqual(
            [message.role for message in result.messages],
            ["system", "user", "assistant", "tool", "assistant"],
        )

    def test_tracer_records_agent_model_tool_and_memory_events(self):
        tracer = InMemoryTracer()
        memory = InMemoryMemory()
        agent = Agent(
            name="TraceAgent",
            instructions="Use tools.",
            model=AddThenAnswerModel(),
            tools=[add],
            memory=memory,
            tracer=tracer,
        )

        agent.run("2 + 4")

        event_names = [event.name for event in tracer.events()]
        self.assertIn("agent.run.start", event_names)
        self.assertIn("memory.load", event_names)
        self.assertIn("model.call.start", event_names)
        self.assertIn("model.call.end", event_names)
        self.assertIn("tool.call.start", event_names)
        self.assertIn("tool.call.end", event_names)
        self.assertIn("memory.save", event_names)
        self.assertIn("agent.run.end", event_names)

    def test_jsonl_tracer_persists_events(self):
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "trace.jsonl"
            tracer = JsonlTracer(path)
            tracer.record(TraceEvent("test.event", {"ok": True}))

            restored = JsonlTracer(path).events()

        self.assertEqual(restored[0].name, "test.event")
        self.assertEqual(restored[0].data, {"ok": True})

    def test_agent_saves_and_loads_in_memory_history(self):
        memory = InMemoryMemory()
        agent = Agent(
            name="MemoryAgent",
            instructions="Remember turns.",
            model=CountingModel(),
            memory=memory,
        )

        first = agent.run("hello")
        second = agent.run("again")

        self.assertEqual(first.output, "user_count=1")
        self.assertEqual(second.output, "user_count=2")
        self.assertEqual([message.role for message in memory.load()], ["user", "assistant", "user", "assistant"])

    def test_agent_history_argument_overrides_memory(self):
        memory = InMemoryMemory([Message(role="user", content="stored")])
        agent = Agent(
            name="MemoryAgent",
            instructions="Remember turns.",
            model=CountingModel(),
            memory=memory,
        )

        result = agent.run("fresh", history=[])

        self.assertEqual(result.output, "user_count=1")

    def test_json_file_memory_persists_messages(self):
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "memory.json"
            memory = JsonFileMemory(path)
            memory.save([Message(role="user", content="hello")])

            restored = JsonFileMemory(path).load()

        self.assertEqual(restored[0].role, "user")
        self.assertEqual(restored[0].content, "hello")

    def test_responses_tool_schema_is_flat(self):
        schema = _tool_to_responses_schema(add.to_openai_schema())

        self.assertEqual(schema["type"], "function")
        self.assertEqual(schema["name"], "add")
        self.assertNotIn("function", schema)

    def test_responses_message_conversion_preserves_tool_call_id(self):
        messages = [
            Message(role="system", content="Use tools."),
            Message(role="user", content="2 + 4"),
            Message(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(id="call_1", name="add", arguments={"a": 2, "b": 4})
                ],
            ),
            Message(role="tool", name="add", tool_call_id="call_1", content="6"),
        ]

        instructions, input_items = _messages_to_responses(messages)

        self.assertEqual(instructions, "Use tools.")
        self.assertEqual(input_items[1]["type"], "function_call")
        self.assertEqual(input_items[1]["call_id"], "call_1")
        self.assertEqual(input_items[2]["type"], "function_call_output")
        self.assertEqual(input_items[2]["call_id"], "call_1")

    def test_responses_output_parser_reads_function_calls(self):
        response = _response_to_model_response(
            {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "add",
                        "arguments": "{\"a\": 2, \"b\": 4}",
                    }
                ]
            }
        )

        self.assertEqual(response.tool_calls[0].id, "call_1")
        self.assertEqual(response.tool_calls[0].name, "add")
        self.assertEqual(response.tool_calls[0].arguments, {"a": 2, "b": 4})

    def test_load_env_file(self):
        with TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text("HELLO_AGENT_TEST='ok'\n", encoding="utf-8")

            load_env_file(env_path, override=True)

        self.assertEqual(os.environ["HELLO_AGENT_TEST"], "ok")

    def test_file_tools_list_read_and_search_inside_root(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "notes").mkdir()
            (root / "notes" / "hello.txt").write_text("Agent tools work.\n", encoding="utf-8")
            registry = {item.name: item for item in make_file_tools(root)}

            self.assertIn("notes/hello.txt", registry["list_files"].run({"pattern": "hello"}))
            self.assertEqual(
                registry["read_text_file"].run({"path": "notes/hello.txt"}),
                "Agent tools work.\n",
            )
            self.assertIn(
                "notes/hello.txt:1",
                registry["search_text"].run({"query": "tools"}),
            )

    def test_file_tools_reject_paths_outside_root(self):
        with TemporaryDirectory() as tmpdir:
            registry = {item.name: item for item in make_file_tools(Path(tmpdir))}

            with self.assertRaises(ValueError):
                registry["read_text_file"].run({"path": "../outside.txt"})


if __name__ == "__main__":
    unittest.main()
