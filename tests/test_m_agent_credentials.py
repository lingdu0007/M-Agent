"""凭证隔离测试（Ticket 02 AC：provider 凭证不进入任何持久化位置）。

ADR 0033：API Key、访问令牌等凭据始终由 Adapter 从外部配置读取，
不进入 Definition Snapshot 或 Run Payload；Trace 默认只记录元数据。
本测试用携带 ``api_key`` 的 Adapter 跑完整成功与失败路径，断言凭证
不出现于：

- Definition Snapshot（可序列化视图）；
- Run Metadata（RunRecord / waiting_reason 等可查询字段）；
- Run Payload 的 Codec 输入（即任何经 Codec 写入的内容）；
- Checkpoint；
- 错误快照（失败 Attempt 的 error）；
- SQLite 数据库文件的原始字节（含 runs / run_payloads 等所有表）。
"""

from __future__ import annotations

import os
import tempfile
import unittest

from m_agent import (
    AgentDefinition,
    DefinitionRegistry,
    DeterministicModelAdapter,
    InMemoryRunStore,
    ModelRequest,
    ModelResponse,
    PlaintextPayloadCodec,
    Runner,
    RunStatus,
    SQLiteRunStore,
    StepStatus,
)

from store_contract import SpyCodec

CREDENTIAL = "sk-test-credential-9f2c1e7a"


class KeyedAdapter(DeterministicModelAdapter):
    """持有 provider 凭证的确定性 adapter（凭证只存在于 adapter 配置）。"""

    deterministic: bool = True

    def __init__(self, api_key: str, responses: tuple[str, ...]) -> None:
        super().__init__(responses=responses)
        self.api_key = api_key


class ExplodingKeyedAdapter(KeyedAdapter):
    """模拟错误 Adapter：把自身凭证误写入裸异常消息。"""

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        raise RuntimeError(f"provider exploded with {self.api_key}")


def build_registry(adapter: DeterministicModelAdapter) -> DefinitionRegistry:
    registry = DefinitionRegistry()
    registry.register(
        AgentDefinition.for_adapter(
            definition_id="assistant",
            version="1.0",
            instructions="Answer deterministically.",
            model_adapter=adapter,
        )
    )
    return registry


def assert_no_credential(testcase: unittest.TestCase, *values: object) -> None:
    for value in values:
        text = value if isinstance(value, str) else str(value)
        testcase.assertNotIn(
            CREDENTIAL,
            text,
            f"credential leaked into persisted data: {text!r}",
        )


def sqlite_file_contains(db_path: str, needle: str) -> bool:
    with open(db_path, "rb") as fh:
        return needle.encode("utf-8") in fh.read()


class CredentialIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def _run_to_terminal(
        self, adapter: DeterministicModelAdapter, store
    ) -> tuple[Runner, str]:
        registry = build_registry(adapter)
        runner = Runner(registry=registry, store=store)
        created = await runner.create_run("assistant", "1.0", input="hi")
        await runner.start_run(created.run_id)
        return runner, created.run_id

    async def test_snapshot_never_contains_credentials(self) -> None:
        adapter = KeyedAdapter(
            api_key=CREDENTIAL, responses=("answer",)
        )
        registry = build_registry(adapter)
        snapshot = registry.resolve("assistant", "1.0").frozen_snapshot()
        assert_no_credential(self, snapshot.model_dump_json())

    async def test_credentials_absent_from_inmemory_run_and_payloads(
        self,
    ) -> None:
        codec = SpyCodec(PlaintextPayloadCodec())
        store = InMemoryRunStore(payload_codec=codec)
        adapter = KeyedAdapter(
            api_key=CREDENTIAL, responses=("safe answer",)
        )
        runner, run_id = await self._run_to_terminal(adapter, store)

        record = await runner.get_run(run_id)
        inspection = await runner.inspect_run(run_id)
        # 凭证仍然只存在于 adapter 配置，不在运行时任何记录里。
        self.assertEqual(adapter.api_key, CREDENTIAL)
        assert_no_credential(
            self,
            record.model_dump_json(),
            inspection.run.model_dump_json(),
            inspection.checkpoints[0].output,
        )
        # 所有经 Codec 编码的 payload 输入都不含凭证。
        for payload in codec.encoded:
            assert_no_credential(self, payload)
        # 原始编码字节不含凭证明文。
        for field in ("run:input", "run:output"):
            raw = store.raw_payload_bytes(run_id, field)
            if raw is not None:
                self.assertNotIn(CREDENTIAL.encode(), raw)
        for checkpoint in inspection.checkpoints:
            raw = store.raw_payload_bytes(
                run_id, f"checkpoint:{checkpoint.step_id}:output"
            )
            if raw is not None:
                self.assertNotIn(CREDENTIAL.encode(), raw)

    async def test_credentials_absent_from_sqlite_file_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "run.db")
            store = SQLiteRunStore(db, payload_codec=PlaintextPayloadCodec())
            adapter = KeyedAdapter(
                api_key=CREDENTIAL, responses=("safe answer",)
            )
            runner, run_id = await self._run_to_terminal(adapter, store)
            record = await runner.get_run(run_id)
            self.assertEqual(record.status, RunStatus.SUCCEEDED)
            store.close()

            # 整个数据库文件（metadata 表 + payload 表 + 索引 + journal）
            # 都不含凭证。
            self.assertFalse(sqlite_file_contains(db, CREDENTIAL))

    async def test_credentials_absent_from_error_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "run.db")
            store = SQLiteRunStore(db, payload_codec=PlaintextPayloadCodec())
            adapter = ExplodingKeyedAdapter(
                api_key=CREDENTIAL, responses=("ignored",)
            )
            runner, run_id = await self._run_to_terminal(adapter, store)
            inspection = await runner.inspect_run(run_id)
            record = await runner.get_run(run_id)
            self.assertEqual(record.status, RunStatus.FAILED)
            attempt = inspection.attempts[0]
            self.assertEqual(attempt.status, StepStatus.FAILED)
            assert_no_credential(
                self, record.model_dump_json(), attempt.error or ""
            )
            self.assertEqual(
                attempt.error, "unclassified adapter exception: RuntimeError"
            )
            store.close()
            self.assertFalse(sqlite_file_contains(db, CREDENTIAL))


if __name__ == "__main__":
    unittest.main()
