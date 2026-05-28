import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_framework import (
    Agent,
    AgentStep,
    ContainsKeywordsEvaluator,
    EvalCase,
    EvalRunner,
    EvalSubjectResult,
    ExactMatchEvaluator,
    ExpectedToolCall,
    SensitiveArgumentGuardrail,
    FunctionStep,
    InMemoryMemory,
    InMemorySessionMemory,
    InMemoryTracer,
    JsonFileMemory,
    JsonDirectorySessionMemory,
    JsonlTracer,
    KeywordRagIndex,
    Message,
    ModelResponse,
    MultiAgentTeam,
    OutputSchema,
    StructuredOutputError,
    TraceEvent,
    ToolCall,
    ToolAllowlistGuardrail,
    ToolDenylistGuardrail,
    StepResult,
    StructuredFieldEvaluator,
    TeamMember,
    ToolTrajectoryEvaluator,
    Workflow,
    load_env_file,
    make_file_tools,
    make_rag_tools,
    parse_structured_output,
    run_agent_evals,
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


class RoleEchoModel:
    def __init__(self, role: str) -> None:
        self.role = role

    def complete(self, messages, tools):
        prompt = next(
            (message.content for message in reversed(messages) if message.role == "user"),
            "",
        )
        return ModelResponse(content=f"{self.role}: {prompt}")


class SecretToolModel:
    def complete(self, messages, tools):
        if messages[-1].role == "tool":
            return ModelResponse(content=messages[-1].content)
        return ModelResponse(
            tool_calls=[
                ToolCall(
                    id="call_1",
                    name="echo_secret",
                    arguments={"secret": "sk-test"},
                )
            ]
        )


@tool
def echo_secret(secret: str) -> str:
    return f"secret={secret}"


class JsonAnswerModel:
    def complete(self, messages, tools):
        return ModelResponse(content='{"answer": "ok", "score": 1}')


class InvalidJsonAnswerModel:
    def complete(self, messages, tools):
        return ModelResponse(content='{"answer": "ok"}')


class RagDemoModel:
    def complete(self, messages, tools):
        if messages[-1].role == "tool":
            return ModelResponse(content=f"rag_answer={messages[-1].content}")
        return ModelResponse(
            tool_calls=[
                ToolCall(
                    id="call_1",
                    name="search_knowledge",
                    arguments={"query": "memory session", "top_k": 1},
                )
            ]
        )


ANSWER_SCHEMA = OutputSchema(
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

    def test_allowlist_guardrail_allows_registered_tool(self):
        agent = Agent(
            name="GuardedAgent",
            instructions="Use tools.",
            model=AddThenAnswerModel(),
            tools=[add],
            guardrails=[ToolAllowlistGuardrail(["add"])],
        )

        result = agent.run("2 + 4")

        self.assertEqual(result.output, "answer=6")

    def test_denylist_guardrail_blocks_tool_execution(self):
        tracer = InMemoryTracer()
        agent = Agent(
            name="GuardedAgent",
            instructions="Use tools.",
            model=AddThenAnswerModel(),
            tools=[add],
            guardrails=[ToolDenylistGuardrail(["add"])],
            tracer=tracer,
        )

        result = agent.run("2 + 4")

        self.assertIn("Tool blocked by guardrail", result.output)
        event_names = [event.name for event in tracer.events()]
        self.assertIn("guardrail.block", event_names)
        self.assertNotIn("tool.call.end", event_names)

    def test_sensitive_argument_guardrail_blocks_matching_key(self):
        agent = Agent(
            name="GuardedAgent",
            instructions="Use tools.",
            model=SecretToolModel(),
            tools=[echo_secret],
            guardrails=[SensitiveArgumentGuardrail(blocked_keys=["secret"])],
        )

        result = agent.run("echo secret")

        self.assertIn("Argument key is blocked: secret", result.output)

    def test_parse_structured_output_validates_schema(self):
        parsed = parse_structured_output(
            'Result: {"answer": "ok", "score": 1}',
            ANSWER_SCHEMA,
        )

        self.assertEqual(parsed, {"answer": "ok", "score": 1})

    def test_parse_structured_output_rejects_missing_required_field(self):
        with self.assertRaises(StructuredOutputError):
            parse_structured_output('{"answer": "ok"}', ANSWER_SCHEMA)

    def test_agent_returns_structured_output(self):
        tracer = InMemoryTracer()
        agent = Agent(
            name="StructuredAgent",
            instructions="Return structured output.",
            model=JsonAnswerModel(),
            tracer=tracer,
        )

        result = agent.run("answer", output_schema=ANSWER_SCHEMA)

        self.assertEqual(result.structured_output, {"answer": "ok", "score": 1})
        self.assertIn("structured_output.valid", [event.name for event in tracer.events()])

    def test_agent_raises_on_invalid_structured_output(self):
        tracer = InMemoryTracer()
        agent = Agent(
            name="StructuredAgent",
            instructions="Return structured output.",
            model=InvalidJsonAnswerModel(),
            tracer=tracer,
        )

        with self.assertRaises(StructuredOutputError):
            agent.run("answer", output_schema=ANSWER_SCHEMA)

        self.assertIn("structured_output.error", [event.name for event in tracer.events()])

    def test_eval_exact_match_evaluator_passes_and_fails(self):
        evaluator = ExactMatchEvaluator()
        passing = evaluator.evaluate(
            EvalCase("exact", "say ok", expected_output="ok"),
            EvalSubjectResult(output="ok"),
        )
        failing = evaluator.evaluate(
            EvalCase("exact", "say ok", expected_output="ok"),
            EvalSubjectResult(output="not ok"),
        )

        self.assertTrue(passing.passed_check)
        self.assertTrue(failing.failed_check)

    def test_eval_contains_keywords_evaluator(self):
        evaluator = ContainsKeywordsEvaluator()
        check = evaluator.evaluate(
            EvalCase(
                "keywords",
                "explain architecture",
                expected_keywords=["models", "tools", "memory"],
            ),
            EvalSubjectResult(
                output="Agent frameworks separate models, tools, memory, and tracing."
            ),
        )

        self.assertTrue(check.passed_check)

    def test_eval_structured_field_evaluator_checks_paths(self):
        evaluator = StructuredFieldEvaluator()
        check = evaluator.evaluate(
            EvalCase(
                "structured",
                "return json",
                expected_structured={"answer": "ok", "metrics.score": 1},
            ),
            EvalSubjectResult(
                output='{"answer": "ok", "metrics": {"score": 1}}',
                structured_output={"answer": "ok", "metrics": {"score": 1}},
            ),
        )

        self.assertTrue(check.passed_check)

    def test_eval_tool_trajectory_evaluator_checks_order_and_arguments(self):
        evaluator = ToolTrajectoryEvaluator()
        check = evaluator.evaluate(
            EvalCase(
                "tool-call",
                "calculate",
                expected_tool_calls=[ExpectedToolCall("add", {"a": 2})],
            ),
            EvalSubjectResult(
                output="answer=6",
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="add",
                        arguments={"a": 2, "b": 4},
                    )
                ],
            ),
        )

        self.assertTrue(check.passed_check)

    def test_eval_tool_trajectory_evaluator_fails_wrong_tool(self):
        evaluator = ToolTrajectoryEvaluator()
        check = evaluator.evaluate(
            EvalCase(
                "tool-call",
                "calculate",
                expected_tool_calls=[ExpectedToolCall("add")],
            ),
            EvalSubjectResult(
                output="time",
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="get_current_time",
                        arguments={},
                    )
                ],
            ),
        )

        self.assertTrue(check.failed_check)

    def test_eval_runner_reports_case_pass_rate(self):
        runner = EvalRunner(lambda case: "Agent tools and memory work.")
        report = runner.run(
            [
                EvalCase(
                    "basic-answer",
                    "explain agent",
                    expected_keywords=["tools", "memory"],
                )
            ]
        )

        self.assertEqual(report.total_cases, 1)
        self.assertEqual(report.passed_cases, 1)
        self.assertEqual(report.pass_rate, 1.0)

    def test_eval_runner_records_target_errors(self):
        def broken_target(case):
            raise RuntimeError("boom")

        report = EvalRunner(broken_target).run([EvalCase("broken", "fail")])

        self.assertEqual(report.failed_cases, 1)
        self.assertIn("RuntimeError: boom", report.results[0].error)

    def test_run_agent_evals_supports_structured_output_cases(self):
        agent = Agent(
            name="EvalAgent",
            instructions="Return structured output.",
            model=JsonAnswerModel(),
        )

        report = run_agent_evals(
            agent,
            [
                EvalCase(
                    "json-answer",
                    "answer",
                    expected_structured={"answer": "ok", "score": 1},
                    output_schema=ANSWER_SCHEMA,
                )
            ],
        )

        self.assertEqual(report.passed_cases, 1)
        self.assertEqual(
            report.results[0].structured_output,
            {"answer": "ok", "score": 1},
        )

    def test_run_agent_evals_supports_tool_trajectory_cases(self):
        agent = Agent(
            name="ToolEvalAgent",
            instructions="Use tools.",
            model=AddThenAnswerModel(),
            tools=[add],
        )

        report = run_agent_evals(
            agent,
            [
                EvalCase(
                    "calculator-tool",
                    "2 + 4",
                    expected_output="answer=6",
                    expected_tool_calls=[
                        ExpectedToolCall("add", {"a": 2, "b": 4})
                    ],
                )
            ],
        )

        self.assertEqual(report.passed_cases, 1)
        self.assertEqual(report.results[0].tool_calls[0]["name"], "add")

    def test_keyword_rag_index_searches_relevant_chunks(self):
        index = KeywordRagIndex(chunk_size=200, overlap=0)
        index.add_text(
            source="memory.md",
            text="Session memory stores separate conversation histories.",
        )
        index.add_text(
            source="tools.md",
            text="Tools expose Python functions to the model.",
        )

        results = index.search("session history", top_k=1)

        self.assertEqual(results[0].chunk.source, "memory.md")

    def test_rag_tools_search_and_read_chunks(self):
        index = KeywordRagIndex(chunk_size=200, overlap=0)
        index.add_text(source="design.md", text="RAG retrieves relevant document chunks.")
        registry = {item.name: item for item in make_rag_tools(index)}

        search_output = registry["search_knowledge"].run({"query": "retrieves chunks"})
        chunk_id = index.search("retrieves chunks", top_k=1)[0].chunk.id
        read_output = registry["read_knowledge_chunk"].run({"chunk_id": chunk_id})

        self.assertIn("design.md", search_output)
        self.assertIn("RAG retrieves", read_output)

    def test_rag_directory_index_ignores_env_and_data(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "docs").mkdir()
            (root / "data").mkdir()
            (root / "docs" / "guide.md").write_text("RAG guide content.", encoding="utf-8")
            (root / ".env").write_text("SECRET=hidden", encoding="utf-8")
            (root / "data" / "memory.json").write_text("private memory", encoding="utf-8")

            index = KeywordRagIndex.from_directory(root)

        sources = [result.chunk.source for result in index.search("RAG guide", top_k=5)]
        self.assertEqual(sources, ["docs/guide.md"])

    def test_agent_can_use_rag_tool(self):
        index = KeywordRagIndex(chunk_size=200, overlap=0)
        index.add_text(
            source="memory.md",
            text="Session memory stores separate conversation histories.",
        )
        agent = Agent(
            name="RagAgent",
            instructions="Use retrieval.",
            model=RagDemoModel(),
            tools=make_rag_tools(index),
        )

        result = agent.run("How does session memory work?")

        self.assertIn("Session memory", result.output)

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

    def test_session_memory_keeps_sessions_isolated(self):
        session_memory = InMemorySessionMemory()
        agent = Agent(
            name="SessionAgent",
            instructions="Remember turns.",
            model=CountingModel(),
            session_memory=session_memory,
        )

        first = agent.run("hello", session_id="alpha")
        second = agent.run("again", session_id="alpha")
        other = agent.run("fresh", session_id="beta")

        self.assertEqual(first.output, "user_count=1")
        self.assertEqual(second.output, "user_count=2")
        self.assertEqual(other.output, "user_count=1")
        self.assertEqual(agent.list_sessions(), ["alpha", "beta"])

    def test_session_memory_clear_and_delete(self):
        session_memory = InMemorySessionMemory()
        agent = Agent(
            name="SessionAgent",
            instructions="Remember turns.",
            model=CountingModel(),
            session_memory=session_memory,
        )

        agent.run("hello", session_id="alpha")
        agent.clear_memory(session_id="alpha")

        self.assertEqual(session_memory.load("alpha"), [])

        agent.run("hello", session_id="beta")
        agent.delete_session("beta")

        self.assertEqual(agent.list_sessions(), [])

    def test_json_directory_session_memory_persists_sessions(self):
        with TemporaryDirectory() as tmpdir:
            memory = JsonDirectorySessionMemory(Path(tmpdir))
            memory.save("alpha/session", [Message(role="user", content="hello")])

            restored = JsonDirectorySessionMemory(Path(tmpdir)).load("alpha/session")
            sessions = JsonDirectorySessionMemory(Path(tmpdir)).list_sessions()

        self.assertEqual(restored[0].content, "hello")
        self.assertEqual(sessions, ["alpha/session"])

    def test_session_memory_emits_trace_events(self):
        tracer = InMemoryTracer()
        agent = Agent(
            name="SessionAgent",
            instructions="Remember turns.",
            model=CountingModel(),
            session_memory=InMemorySessionMemory(),
            tracer=tracer,
        )

        agent.run("hello", session_id="alpha")

        event_names = [event.name for event in tracer.events()]
        self.assertIn("session.load", event_names)
        self.assertIn("session.save", event_names)

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

    def test_workflow_runs_linear_function_steps(self):
        workflow = Workflow(
            [
                FunctionStep("first", lambda context: {"value": 1}),
                FunctionStep("second", lambda context: {"value": context["value"] + 1}),
            ],
            start="first",
        )

        result = workflow.run()

        self.assertEqual(result["value"], 2)

    def test_workflow_supports_conditional_next_step(self):
        def choose(context):
            if context["route"] == "right":
                return StepResult.next("right", chosen=True)
            return StepResult.next("left", chosen=False)

        workflow = Workflow(
            [
                FunctionStep("choose", choose),
                FunctionStep("left", lambda context: StepResult.done(side="left")),
                FunctionStep("right", lambda context: StepResult.done(side="right")),
            ],
            start="choose",
        )

        result = workflow.run({"route": "right"})

        self.assertEqual(result["side"], "right")

    def test_workflow_agent_step_stores_agent_output(self):
        agent = Agent(
            name="WorkflowAgent",
            instructions="Answer.",
            model=CountingModel(),
        )
        workflow = Workflow(
            [
                AgentStep(
                    "ask_agent",
                    agent,
                    lambda context: "hello",
                    output_key="answer",
                )
            ],
            start="ask_agent",
        )

        result = workflow.run()

        self.assertEqual(result["answer"], "user_count=1")

    def test_workflow_emits_trace_events(self):
        tracer = InMemoryTracer()
        workflow = Workflow(
            [FunctionStep("first", lambda context: {"ok": True})],
            start="first",
            tracer=tracer,
        )

        workflow.run()

        event_names = [event.name for event in tracer.events()]
        self.assertIn("workflow.run.start", event_names)
        self.assertIn("workflow.step.start", event_names)
        self.assertIn("workflow.step.end", event_names)
        self.assertIn("workflow.run.end", event_names)

    def test_workflow_stops_at_max_steps(self):
        workflow = Workflow(
            [FunctionStep("loop", lambda context: StepResult.next("loop"))],
            start="loop",
            max_steps=2,
        )

        with self.assertRaises(RuntimeError):
            workflow.run()

    def test_multi_agent_team_runs_members_in_order(self):
        researcher = Agent(
            name="Researcher",
            instructions="Research.",
            model=RoleEchoModel("research"),
        )
        writer = Agent(
            name="Writer",
            instructions="Write.",
            model=RoleEchoModel("write"),
        )
        team = MultiAgentTeam(
            [
                TeamMember(
                    name="researcher",
                    role="Research facts",
                    agent=researcher,
                    prompt=lambda context: f"Find facts for {context['task']}",
                    output_key="research",
                ),
                TeamMember(
                    name="writer",
                    role="Write final answer",
                    agent=writer,
                    prompt=lambda context: f"Use this research: {context['research']}",
                    output_key="final",
                ),
            ],
            final_output_key="final",
        )

        result = team.run("Agent memory")

        self.assertIn("research: Find facts", result.context["research"])
        self.assertIn("write: Use this research", result.final_output)
        self.assertEqual(list(result.member_outputs.keys()), ["research", "final"])

    def test_multi_agent_team_emits_trace_events(self):
        tracer = InMemoryTracer()
        agent = Agent(name="Solo", instructions="Answer.", model=RoleEchoModel("solo"))
        team = MultiAgentTeam(
            [
                TeamMember(
                    name="solo",
                    role="Answer",
                    agent=agent,
                    prompt=lambda context: context["task"],
                )
            ],
            tracer=tracer,
        )

        team.run("hello")

        event_names = [event.name for event in tracer.events()]
        self.assertIn("multi_agent.run.start", event_names)
        self.assertIn("multi_agent.member.start", event_names)
        self.assertIn("multi_agent.member.end", event_names)
        self.assertIn("multi_agent.run.end", event_names)

    def test_multi_agent_team_requires_members(self):
        with self.assertRaises(ValueError):
            MultiAgentTeam([])


if __name__ == "__main__":
    unittest.main()
