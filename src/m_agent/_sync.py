"""Blocking facade for the async-first :class:`m_agent.Runner`."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine, Sequence
from concurrent.futures import Future
from typing import Any, TypeVar

from ._resolution import RunResolution
from ._history import ConversationMessage
from ._run import RunInspection, RunRecord
from ._runner import Runner

_T = TypeVar("_T")


class SyncRunner:
    """Delegate Runner commands to one background event loop.

    The wrapper intentionally exposes the async Runner's records and errors;
    it is only an event-loop bridge for scripts and synchronous applications.
    """

    def __init__(self, runner: Runner) -> None:
        self._runner = runner
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._closed = False
        self._thread = threading.Thread(
            target=self._run_loop,
            name="m-agent-sync-runner",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait()

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._ready.set()
        loop.run_forever()
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()

    def _call(self, coroutine: Coroutine[Any, Any, _T]) -> _T:
        if self._closed or self._loop is None:
            coroutine.close()
            raise RuntimeError("SyncRunner is closed")
        future: Future[_T] = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        return future.result()

    @property
    def owner(self) -> str:
        return self._runner.owner

    def create_run(
        self,
        definition_id: str,
        version: str,
        input: str,
        *,
        run_id: str | None = None,
        history: Sequence[ConversationMessage] = (),
    ) -> RunRecord:
        return self._call(
            self._runner.create_run(
                definition_id,
                version,
                input,
                run_id=run_id,
                history=history,
            )
        )

    def start_run(self, run_id: str) -> RunRecord:
        return self._call(self._runner.start_run(run_id))

    def resume_run(self, run_id: str) -> RunRecord:
        return self._call(self._runner.resume_run(run_id))

    def get_run(self, run_id: str) -> RunRecord:
        return self._call(self._runner.get_run(run_id))

    def inspect_run(self, run_id: str) -> RunInspection:
        return self._call(self._runner.inspect_run(run_id))

    def cancel_run(self, run_id: str, expected_version: int | None = None) -> RunRecord:
        return self._call(self._runner.cancel_run(run_id, expected_version))

    def resolve_run(
        self,
        run_id: str,
        resolution: RunResolution,
        expected_version: int,
    ) -> RunRecord:
        return self._call(self._runner.resolve_run(run_id, resolution, expected_version))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            raise RuntimeError("SyncRunner event loop did not stop")

    def __enter__(self) -> "SyncRunner":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
