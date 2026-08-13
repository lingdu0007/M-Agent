"""Ticket 02 的受保护 Payload 跨进程测试 worker。"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import timedelta

_SRC = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "src")
)
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from m_agent import (  # noqa: E402
    AgentDefinition,
    CrashPoint,
    DefinitionRegistry,
    DeterministicModelAdapter,
    FakeClock,
    ModelRequest,
    ModelResponse,
    PayloadCodec,
    Runner,
    SQLiteRunStore,
)

_CRASH_EXIT_CODE = 17
_PREFIX = b"ticket02-sentinel:"


class SentinelPayloadCodec(PayloadCodec):
    """可逆但非恒等的测试 Codec，避免 sentinel 以原样进入 SQLite。"""

    name = "ticket02-sentinel"

    def encode(self, payload: str) -> bytes:
        return _PREFIX + bytes(byte ^ 0xA5 for byte in payload.encode("utf-8"))

    def decode(self, encoded: bytes) -> str:
        if not encoded.startswith(_PREFIX):
            raise ValueError("unexpected Ticket 02 test payload encoding")
        return bytes(byte ^ 0xA5 for byte in encoded[len(_PREFIX) :]).decode(
            "utf-8"
        )


class LoggingModelAdapter(DeterministicModelAdapter):
    def __init__(self, log_path: str, response: str) -> None:
        super().__init__(responses=(response,))
        self._log_path = log_path

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        self._last_request = request
        with open(self._log_path, "a", encoding="utf-8") as fh:
            fh.write("model-call\n")
        return ModelResponse(content=self._responses[0])


def _registry(log_path: str, instructions: str, response: str) -> DefinitionRegistry:
    registry = DefinitionRegistry()
    registry.register(
        AgentDefinition(
            definition_id="sentinel-assistant",
            version="1.0",
            instructions=instructions,
            model_adapter=LoggingModelAdapter(log_path, response),
        )
    )
    return registry


async def _crash(db_path: str, log_path: str, instructions: str, response: str) -> None:
    def crash_hook(point: CrashPoint, run_id: str) -> None:
        if point is CrashPoint.AFTER_MODEL_CHECKPOINT:
            print(f"RUN_ID={run_id}", flush=True)
            os._exit(_CRASH_EXIT_CODE)

    store = SQLiteRunStore(db_path, payload_codec=SentinelPayloadCodec())
    try:
        runner = Runner(
            registry=_registry(log_path, instructions, response),
            store=store,
            crash_hook=crash_hook,
        )
        created = await runner.create_run(
            "sentinel-assistant", "1.0", input="sentinel input"
        )
        await runner.start_run(created.run_id)
    finally:
        store.close()


async def _resume(
    db_path: str, log_path: str, run_id: str, instructions: str, response: str
) -> None:
    probe = SQLiteRunStore(db_path, payload_codec=SentinelPayloadCodec())
    try:
        record = await probe.get_run(run_id)
        if record is None or record.lease_expires_at is None:
            raise RuntimeError("crashed run has no persisted lease")
        clock = FakeClock(start=record.lease_expires_at + timedelta(seconds=1))
    finally:
        probe.close()

    store = SQLiteRunStore(
        db_path, payload_codec=SentinelPayloadCodec(), clock=clock
    )
    try:
        registry = _registry(log_path, instructions, response)
        adapter = registry.resolve("sentinel-assistant", "1.0").model_adapter
        result = await Runner(registry=registry, store=store).resume_run(run_id)
        print(f"STATUS={result.status.value}")
        print(f"OUTPUT={result.output}")
        print(f"INSTRUCTIONS={result.snapshot.instructions}")
        print(f"MODEL_CALLS={adapter.call_count}")
    finally:
        store.close()


def main() -> None:
    mode, db_path, log_path, *values = sys.argv[1:]
    if mode == "crash":
        instructions, response = values
        asyncio.run(_crash(db_path, log_path, instructions, response))
        return
    if mode == "resume":
        run_id, instructions, response = values
        asyncio.run(_resume(db_path, log_path, run_id, instructions, response))
        return
    raise ValueError(f"unknown mode: {mode}")


if __name__ == "__main__":
    main()
