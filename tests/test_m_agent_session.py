"""一条 scoped Session 对话（InMemory SessionStore 绑定）。

本文件把实现无关的共享契约套件绑定到 InMemorySessionStore，并覆盖
Core 侧 Conversation History seam 的最小修复：

- ``Runner.create_run`` 接受预分配 run identity 与冻结 history；
- history 作为受保护 Run Payload 经 PayloadCodec 持久化
  （InMemory 与 SQLite 双实现）；
- ModelRequest 携带冻结 history，sessionless Run 语义不变。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from m_agent.adapters import (
    DeterministicModelAdapter,
    InMemoryRunStore,
    PlaintextPayloadCodec,
    SQLiteRunStore,
)
from m_agent.companion import (
    InMemorySessionStore,
    SessionRunner,
)
from m_agent.runtime import (
    AgentDefinition,
    ConversationMessage,
    ConversationRole,
    DefinitionRegistry,
    DuplicateRunError,
    ModelRequest,
    ModelResponse,
    RunStatus,
    Runner,
)
from session_conversation_contract import SessionConversationContractMixin
from session_store_contract import SessionStoreContractMixin
from store_contract import SpyCodec

_HISTORY = (
    ConversationMessage(role=ConversationRole.USER, content="earlier question"),
    ConversationMessage(role=ConversationRole.ASSISTANT, content="earlier answer"),
)


class _RecordingAdapter(DeterministicModelAdapter):
    deterministic: bool = True

    def __init__(self) -> None:
        super().__init__(responses=("answer",))
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        self._last_request = request
        self.requests.append(request)
        return ModelResponse(content="answer")


class InMemorySessionStoreContractTests(
    SessionStoreContractMixin, unittest.IsolatedAsyncioTestCase
):
    """AC 9：共享 SessionStore 契约套件的 InMemory 绑定。"""

    def make_store(self, clock=None):
        return InMemorySessionStore(clock=clock)


class InMemorySessionConversationTests(
    SessionConversationContractMixin, unittest.IsolatedAsyncioTestCase
):
    """AC 5-8：共享组合行为契约套件的 InMemory 绑定。"""

    def make_session_store(self):
        return InMemorySessionStore()


class ConversationHistorySeamTests(unittest.IsolatedAsyncioTestCase):
    """Core seam：Conversation History 冻结、受保护持久化与模型交付。"""

    def _make_runner(self, store) -> tuple[Runner, _RecordingAdapter]:
        adapter = _RecordingAdapter()
        registry = DefinitionRegistry()
        registry.register(
            AgentDefinition.for_adapter(
                definition_id="assistant",
                version="1.0",
                instructions="x",
                model_adapter=adapter,
            )
        )
        return Runner(registry=registry, store=store), adapter

    async def test_create_run_accepts_preallocated_identity_and_history(self) -> None:
        runner, _ = self._make_runner(InMemoryRunStore(PlaintextPayloadCodec()))
        created = await runner.create_run(
            "assistant",
            "1.0",
            "new message",
            run_id="preallocated-run",
            history=_HISTORY,
        )
        self.assertEqual(created.run_id, "preallocated-run")
        self.assertEqual(created.history, _HISTORY)
        self.assertEqual(created.input, "new message")
        self.assertEqual(created.status, RunStatus.CREATED)

        # 预分配 identity 落库后被占用：重复创建确定性失败。
        with self.assertRaises(DuplicateRunError):
            await runner.create_run(
                "assistant",
                "1.0",
                "other",
                run_id="preallocated-run",
                history=(),
            )

    async def test_create_run_rejects_blank_preallocated_identity(self) -> None:
        runner, _ = self._make_runner(InMemoryRunStore(PlaintextPayloadCodec()))
        with self.assertRaises(ValueError):
            await runner.create_run(
                "assistant", "1.0", "input", run_id="   ", history=()
            )

    async def test_history_is_protected_payload_roundtrip_in_memory(self) -> None:
        codec = SpyCodec(PlaintextPayloadCodec())
        store = InMemoryRunStore(payload_codec=codec)
        runner, _ = self._make_runner(store)

        created = await runner.create_run(
            "assistant", "1.0", "new message", run_id="run-h", history=_HISTORY
        )
        # 持久化字节确实经过 Codec：带 plaintext 前缀标记，而不是裸 JSON。
        raw = store.raw_payload_bytes("run-h", "run:history")
        self.assertIsNotNone(raw)
        self.assertTrue(raw.startswith(b"m-agent-plaintext:"))
        decoded = json.loads(PlaintextPayloadCodec().decode(raw))
        self.assertEqual(
            [message["role"] for message in decoded],
            ["USER", "ASSISTANT"],
        )
        self.assertEqual(
            [message["content"] for message in decoded],
            ["earlier question", "earlier answer"],
        )
        # 写入路径经过配置的 Codec（Spy 记录到编码前的明文 JSON）。
        self.assertTrue(
            any("earlier question" in item for item in codec.encoded)
        )

        # 读取路径经解码完整还原冻结 history。
        restored = await store.get_run("run-h")
        self.assertIsNotNone(restored)
        self.assertEqual(restored.history, _HISTORY)
        self.assertEqual(restored.input, "new message")
        self.assertEqual(created.history, _HISTORY)

    async def test_history_roundtrips_through_sqlite_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "history.db")
            store = SQLiteRunStore(database, payload_codec=PlaintextPayloadCodec())
            runner, _ = self._make_runner(store)
            await runner.create_run(
                "assistant", "1.0", "new message", run_id="run-h", history=_HISTORY
            )
            store.close()

            reopened = SQLiteRunStore(
                database, payload_codec=PlaintextPayloadCodec()
            )
            try:
                restored = await reopened.get_run("run-h")
                self.assertIsNotNone(restored)
                self.assertEqual(restored.history, _HISTORY)
                raw = reopened.raw_payload_bytes("run-h", "run:history")
                self.assertIsNotNone(raw)
                self.assertTrue(
                    raw.startswith(b"m-agent-plaintext:")
                )
            finally:
                reopened.close()

    async def test_empty_history_omits_payload_field(self) -> None:
        store = InMemoryRunStore(PlaintextPayloadCodec())
        runner, _ = self._make_runner(store)
        await runner.create_run("assistant", "1.0", "plain input", run_id="run-plain")
        self.assertIsNone(store.raw_payload_bytes("run-plain", "run:history"))
        restored = await store.get_run("run-plain")
        self.assertIsNotNone(restored)
        self.assertEqual(restored.history, ())

    async def test_model_request_receives_frozen_history(self) -> None:
        store = InMemoryRunStore(PlaintextPayloadCodec())
        runner, adapter = self._make_runner(store)
        created = await runner.create_run(
            "assistant", "1.0", "new message", history=_HISTORY
        )
        terminal = await runner.start_run(created.run_id)
        self.assertIs(terminal.status, RunStatus.SUCCEEDED)
        self.assertEqual(adapter.requests[-1].history, _HISTORY)
        self.assertEqual(adapter.requests[-1].input, "new message")

    async def test_sessionless_run_has_empty_history_by_default(self) -> None:
        store = InMemoryRunStore(PlaintextPayloadCodec())
        runner, adapter = self._make_runner(store)
        created = await runner.create_run("assistant", "1.0", "just input")
        terminal = await runner.start_run(created.run_id)
        self.assertIs(terminal.status, RunStatus.SUCCEEDED)
        self.assertEqual(created.history, ())
        self.assertEqual(adapter.requests[-1].history, ())


class CompanionLayerTests(unittest.TestCase):
    """companion 层公共表面与依赖方向。"""

    def test_session_contracts_are_public_in_companion(self) -> None:
        import m_agent
        import m_agent.companion as companion

        expected = {
            "DuplicateSessionError",
            "InMemorySessionStore",
            "SessionClaimConflictError",
            "SessionCommitResult",
            "SessionCommitStatus",
            "SessionError",
            "SessionNotFoundError",
            "SessionRecord",
            "SessionRunClaim",
            "SessionRunResult",
            "SessionRunner",
            "SessionScope",
            "SessionSnapshot",
            "SessionStore",
            "SessionTurn",
            "SessionVersionConflictError",
        }
        self.assertTrue(expected.issubset(set(companion.__all__)))
        # Session 能力不进入 root facade，也不进入 runtime Core。
        self.assertFalse(hasattr(m_agent, "SessionRunner"))
        import m_agent.runtime as runtime

        self.assertFalse(hasattr(runtime, "SessionRunner"))
        self.assertTrue(hasattr(runtime, "ConversationMessage"))
        self.assertTrue(hasattr(runtime, "ConversationRole"))
        # Companion 只依赖 Core，Core 不反向导入 Companion。
        self.assertTrue(
            SessionRunner.__module__.startswith("m_agent.companion")
        )
        self.assertTrue(
            InMemorySessionStore.__module__.startswith("m_agent.companion")
        )


if __name__ == "__main__":
    unittest.main()
