"""Correctness-first benchmark for durable Session conversation workloads.

The measured interval covers public ``SessionRunner.submit`` calls for every
Turn of every Session: concurrent conversations, sequential Turns per
conversation. History/version/claim integrity validation, reopen comparison,
and SQLite validation run after the interval and are never included in the
reported performance metrics.

Session history is protected by the Session PayloadCodec boundary
(``PlaintextPayloadCodec`` here: a documented development/test codec, mirroring
the Durable Run benchmark); it is configured independently from the RunStore
PayloadCodec.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path
from statistics import fmean
from typing import Any

from m_agent.adapters import (
    DeterministicModelAdapter,
    PlaintextPayloadCodec,
    SQLiteRunStore,
)
from m_agent.companion import (
    SessionScope,
    SessionSnapshot,
    SessionRunner,
    SQLiteSessionStore,
)
from m_agent.runtime import (
    AgentDefinition,
    DefinitionRegistry,
    ModelRequest,
    ModelResponse,
    Runner,
    RunStatus,
)


MEASURED_SESSIONS = 10
TURNS_PER_SESSION = 10
DEFINITION_ID = "session-workload"
DEFINITION_VERSION = "1.0"
_SCOPE_TOKEN = "session-workload-scope"


class SessionWorkloadValidationError(RuntimeError):
    """Correctness failed, so no performance metrics may be reported."""

    def __init__(self, validation: dict[str, Any]) -> None:
        self.payload = {"validation": validation}
        super().__init__(json.dumps(self.payload, sort_keys=True))


def compare_with_baseline(
    report: dict[str, Any], baseline: dict[str, Any]
) -> dict[str, Any]:
    """Compare only compatible correctness-validated local environments."""
    report_env = report.get("environment", {})
    baseline_env = baseline.get("environment", {})
    report_identity = report.get("baseline_identity", {})
    baseline_identity = baseline.get("baseline_identity", {})
    compatible = (
        report_env.get("python", {}).get("implementation")
        == baseline_env.get("python", {}).get("implementation")
        and report_env.get("python", {}).get("version")
        == baseline_env.get("python", {}).get("version")
        and report_env.get("os", {}).get("system")
        == baseline_env.get("os", {}).get("system")
        and report_env.get("cpu", {}).get("machine")
        == baseline_env.get("cpu", {}).get("machine")
        and report.get("validation", {}).get("status", "PASS") == "PASS"
        and baseline.get("validation", {}).get("status", "PASS") == "PASS"
        and report.get("measurement", {}).get("turn_count")
        == baseline.get("measurement", {}).get("turn_count")
        and bool(report_identity.get("artifact_digest"))
        and bool(report_identity.get("manifest_digest"))
        and report_identity == baseline_identity
    )
    result: dict[str, Any] = {
        "status": "COMPARED" if compatible else "INCONCLUSIVE",
        "qualification": "environment-qualified local evidence only",
        "non_claim": "This comparison is not production QPS, SLA, or capacity evidence.",
    }
    if not compatible:
        result["reason"] = "incompatible environment or correctness validation"
        return result
    current = report["metrics"]["throughput_turns_per_second"]
    previous = baseline["metrics"]["throughput_turns_per_second"]
    result["metrics"] = {
        "throughput_delta_ratio": (current - previous) / previous
        if previous
        else None,
    }
    return result


class SessionWorkloadModel(DeterministicModelAdapter):
    """Stateless deterministic adapter echoing the input as the final output."""

    deterministic: bool = True

    def __init__(self) -> None:
        super().__init__(responses=("unused",))

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        self._last_request = request
        return ModelResponse(content=f"acknowledged:{request.input}")


class TimedSQLiteSessionStore(SQLiteSessionStore):
    """SQLiteSessionStore that times the two writes forming each Turn."""

    def __init__(self, path: Path) -> None:
        super().__init__(path, payload_codec=PlaintextPayloadCodec())
        self.turn_write_seconds: dict[str, float] = defaultdict(float)

    async def claim_run(self, scope, session_id, run_id, *, expected_version):
        started = time.perf_counter()
        try:
            return await super().claim_run(
                scope, session_id, run_id, expected_version=expected_version
            )
        finally:
            self.turn_write_seconds[run_id] += time.perf_counter() - started

    async def commit_turn(self, scope, session_id, turn, *, expected_version):
        started = time.perf_counter()
        try:
            return await super().commit_turn(
                scope, session_id, turn, expected_version=expected_version
            )
        finally:
            self.turn_write_seconds[turn.run_id] += (
                time.perf_counter() - started
            )


class AsyncStartBarrier:
    """Release all benchmark workers only after every worker is ready."""

    def __init__(self, parties: int) -> None:
        self._parties = parties
        self._ready = 0
        self._condition = asyncio.Condition()
        self._release = asyncio.Event()

    async def wait(self) -> None:
        async with self._condition:
            self._ready += 1
            self._condition.notify_all()
        await self._release.wait()

    async def release_when_ready(self) -> None:
        async with self._condition:
            await self._condition.wait_for(
                lambda: self._ready == self._parties
            )
        self._release.set()


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile requires at least one value")
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _environment() -> dict[str, Any]:
    uname = platform.uname()
    return {
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
        },
        "os": {
            "system": uname.system,
            "release": uname.release,
            "version": uname.version,
        },
        "cpu": {
            "machine": uname.machine,
            "processor": uname.processor or platform.processor() or "unknown",
            "logical_count": os.cpu_count(),
        },
    }


def _session_database_evidence(path: Path) -> dict[str, Any]:
    with sqlite3.connect(path) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        synchronous = connection.execute("PRAGMA synchronous").fetchone()[0]
        counts = {
            table: connection.execute(
                f"SELECT COUNT(*) FROM {table}"  # table names are fixed below
            ).fetchone()[0]
            for table in ("sessions", "session_claims", "session_turns")
        }
        orphan_turns = connection.execute(
            "SELECT COUNT(*) FROM session_turns t LEFT JOIN sessions s "
            "ON s.scope_token=t.scope_token AND s.session_id=t.session_id "
            "WHERE s.session_id IS NULL"
        ).fetchone()[0]
        orphan_claims = connection.execute(
            "SELECT COUNT(*) FROM session_claims c LEFT JOIN sessions s "
            "ON s.scope_token=c.scope_token AND s.session_id=c.session_id "
            "WHERE s.session_id IS NULL"
        ).fetchone()[0]
        scope_leaks = connection.execute(
            "SELECT COUNT(DISTINCT scope_token) FROM session_turns "
            "WHERE scope_token != ?",
            (_SCOPE_TOKEN,),
        ).fetchone()[0]
    return {
        "path": str(path),
        "journal_mode": journal_mode,
        "synchronous": synchronous,
        "integrity_check": integrity,
        "row_counts": counts,
        "orphan_counts": {
            "turns_without_session": orphan_turns,
            "claims_without_session": orphan_claims,
        },
        "foreign_scope_turns": scope_leaks,
    }


def _run_database_evidence(path: Path) -> dict[str, Any]:
    with sqlite3.connect(path) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        runs = connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    return {"path": str(path), "integrity_check": integrity, "row_counts": {"runs": runs}}


def validate_session_history(
    snapshot: SessionSnapshot, *, expected_inputs: list[str]
) -> list[str]:
    """Validate one Session's history, version, and claim integrity."""
    problems: list[str] = []
    if snapshot.version != len(snapshot.turns):
        problems.append(
            f"{snapshot.session_id}: version {snapshot.version} != turn count"
            f" {len(snapshot.turns)}"
        )
    if len(snapshot.turns) != len(expected_inputs):
        problems.append(
            f"{snapshot.session_id}: unexpected turn count "
            f"{len(snapshot.turns)}"
        )
    run_ids = [turn.run_id for turn in snapshot.turns]
    if len(set(run_ids)) != len(run_ids):
        problems.append(f"{snapshot.session_id}: duplicate turn run identity")
    for turn, expected_input in zip(
        snapshot.turns, expected_inputs, strict=False
    ):
        if turn.user_input != expected_input:
            problems.append(f"{snapshot.session_id}: turn input mismatch")
        if turn.assistant_output != f"acknowledged:{expected_input}":
            problems.append(f"{snapshot.session_id}: turn output mismatch")
        if turn.definition_id != DEFINITION_ID:
            problems.append(f"{snapshot.session_id}: unexpected definition id")
    return problems


def _history_digest(snapshots: dict[str, SessionSnapshot]) -> str:
    payload = {
        session_id: [
            {
                "run_id": turn.run_id,
                "turn_id": turn.turn_id,
                "user_input": turn.user_input,
                "assistant_output": turn.assistant_output,
            }
            for turn in snapshot.turns
        ]
        for session_id, snapshot in sorted(snapshots.items())
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _registry() -> DefinitionRegistry:
    registry = DefinitionRegistry()
    registry.register(
        AgentDefinition.for_adapter(
            definition_id=DEFINITION_ID,
            version=DEFINITION_VERSION,
            instructions="echo the input deterministically",
            model_adapter=SessionWorkloadModel(),
        )
    )
    return registry


async def execute_session_workload(
    *,
    session_database: Path,
    run_database: Path,
    session_count: int,
    turns_per_session: int,
) -> dict[str, Any]:
    """Execute measured Turns, validate correctness, then calculate metrics."""
    if session_count < 1 or turns_per_session < 1:
        raise ValueError("session_count and turns_per_session must be positive")
    for database in (session_database, run_database):
        if database.exists():
            raise FileExistsError(
                f"refusing to overwrite existing benchmark database: {database}"
            )
        database.parent.mkdir(parents=True, exist_ok=True)

    session_store = TimedSQLiteSessionStore(session_database)
    run_store = SQLiteRunStore(run_database, payload_codec=PlaintextPayloadCodec())
    runner = SessionRunner(
        runner=Runner(_registry(), run_store), session_store=session_store
    )
    scope = SessionScope(token=_SCOPE_TOKEN)
    expected_inputs: dict[str, list[str]] = {}
    barrier = AsyncStartBarrier(session_count)
    latencies: dict[str, float] = {}

    async def drive_session(index: int) -> None:
        session_id = f"session-workload-{index:03d}"
        await session_store.create_session(scope, session_id)
        inputs = [f"turn-{index:03d}-{turn:02d}" for turn in range(turns_per_session)]
        expected_inputs[session_id] = inputs
        await barrier.wait()
        for turn_input in inputs:
            started = time.perf_counter()
            result = await runner.submit(
                scope, session_id, DEFINITION_ID, DEFINITION_VERSION, turn_input
            )
            latencies[result.run.run_id] = time.perf_counter() - started

    tasks = [
        asyncio.create_task(drive_session(index)) for index in range(session_count)
    ]
    await barrier.release_when_ready()
    measured_started = time.perf_counter()
    await asyncio.gather(*tasks)
    measured_seconds = time.perf_counter() - measured_started
    turn_count = session_count * turns_per_session

    problems: list[str] = []
    snapshots: dict[str, SessionSnapshot] = {}
    succeeded_runs = 0
    for session_id, inputs in expected_inputs.items():
        record = await session_store.get_session(scope, session_id)
        if record is None:
            problems.append(f"{session_id}: session record missing")
            continue
        if record.claim is not None:
            problems.append(f"{session_id}: residual claim after workload")
        if record.version != turns_per_session:
            problems.append(f"{session_id}: version != turn count")
        snapshot = await session_store.read_snapshot(scope, session_id)
        snapshots[session_id] = snapshot
        problems.extend(validate_session_history(snapshot, expected_inputs=inputs))
        for turn in snapshot.turns:
            run = await run_store.get_run(turn.run_id)
            if run is None or run.status is not RunStatus.SUCCEEDED:
                problems.append(f"{session_id}: turn run is not SUCCEEDED")
            else:
                succeeded_runs += 1

    reopen_digest = _history_digest(snapshots)
    session_store.close()
    run_store.close()

    reopened_sessions = SQLiteSessionStore(
        session_database, payload_codec=PlaintextPayloadCodec()
    )
    reopened_runs = SQLiteRunStore(
        run_database, payload_codec=PlaintextPayloadCodec()
    )
    try:
        reopened_snapshots = {
            session_id: await reopened_sessions.read_snapshot(scope, session_id)
            for session_id in expected_inputs
        }
        if _history_digest(reopened_snapshots) != reopen_digest:
            problems.append("reopened history does not match the measured history")
        reopened_record = await reopened_sessions.get_session(
            scope, f"session-workload-000"
        )
        if reopened_record is None or reopened_record.claim is not None:
            problems.append("reopened claim state is not clean")
    finally:
        reopened_sessions.close()
        reopened_runs.close()

    session_database_evidence = _session_database_evidence(session_database)
    run_database_evidence = _run_database_evidence(run_database)
    expected_rows = {
        "sessions": session_count,
        "session_claims": 0,
        "session_turns": turn_count,
    }
    if session_database_evidence["row_counts"] != expected_rows:
        problems.append("session SQLite row counts do not match the public views")
    if session_database_evidence["integrity_check"] != "ok":
        problems.append("session SQLite integrity_check did not return ok")
    if any(session_database_evidence["orphan_counts"].values()):
        problems.append("session SQLite contains orphaned lifecycle records")
    if session_database_evidence["foreign_scope_turns"]:
        problems.append("session SQLite contains turns outside the workload scope")
    if run_database_evidence["row_counts"]["runs"] != turn_count:
        problems.append("run SQLite row counts do not match the public views")
    if run_database_evidence["integrity_check"] != "ok":
        problems.append("run SQLite integrity_check did not return ok")

    validation = {
        "status": "FAIL" if problems else "PASS",
        "problems": problems,
        "sessions": session_count,
        "turns": turn_count,
        "terminal_runs": succeeded_runs,
    }
    if problems:
        raise SessionWorkloadValidationError(validation)

    latency_ms = [seconds * 1000 for seconds in latencies.values()]
    persistence_ms = [
        seconds * 1000
        for run_id, seconds in session_store.turn_write_seconds.items()
        if run_id in latencies
    ]
    report = {
        "schema_version": 1,
        "environment": _environment(),
        "baseline_identity": {
            "artifact_digest": os.environ.get(
                "M_AGENT_BENCHMARK_ARTIFACT_DIGEST", ""
            ),
            "manifest_digest": os.environ.get(
                "M_AGENT_BENCHMARK_MANIFEST_DIGEST", ""
            ),
        },
        "warmup": {
            "turn_count": 0,
            "definition": "SQLite schema initialization before measurement",
        },
        "measurement": {
            "session_count": session_count,
            "turns_per_session": turns_per_session,
            "turn_count": turn_count,
            "concurrency": session_count,
            "definition": (
                "wall clock from barrier release through concurrent public "
                "SessionRunner.submit completion for every Turn; validation "
                "and inspection excluded"
            ),
            "elapsed_seconds": measured_seconds,
        },
        "session_database": session_database_evidence,
        "run_database": run_database_evidence,
        "validation": validation,
        "metrics": {
            "throughput_turns_per_second": turn_count / measured_seconds,
            "turn_latency_ms": {
                "p50": _percentile(latency_ms, 0.50),
                "p95": _percentile(latency_ms, 0.95),
            },
            "persistence_overhead_ms_per_turn": {
                "mean": fmean(persistence_ms),
                "p50": _percentile(persistence_ms, 0.50),
                "p95": _percentile(persistence_ms, 0.95),
                "definition": (
                    "summed claim_run and commit_turn wall time for each "
                    "committed Turn"
                ),
            },
        },
        "qualification": (
            "Comparative local evidence only; not production QPS, latency, "
            "scalability, or availability proof."
        ),
    }
    return report


def format_report(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    latency = metrics["turn_latency_ms"]
    persistence = metrics["persistence_overhead_ms_per_turn"]
    environment = report["environment"]
    session_database = report["session_database"]
    measurement = report["measurement"]
    return "\n".join(
        (
            "Durable Session conversation SQLite benchmark",
            f"VALIDATION: {report['validation']['status']}",
            f"Sessions/Turns per session: {measurement['session_count']}/"
            f"{measurement['turns_per_session']}",
            f"Throughput: {metrics['throughput_turns_per_second']:.2f} Turns/s",
            f"Turn latency P50/P95: {latency['p50']:.2f}/"
            f"{latency['p95']:.2f} ms",
            f"Persistence overhead per Turn mean/P50/P95: "
            f"{persistence['mean']:.3f}/{persistence['p50']:.3f}/"
            f"{persistence['p95']:.3f} ms",
            f"Python: {environment['python']['implementation']} "
            f"{environment['python']['version']}",
            f"OS/CPU: {environment['os']['system']} "
            f"{environment['os']['release']} / "
            f"{environment['cpu']['machine']} "
            f"({environment['cpu']['logical_count']} logical CPUs)",
            f"SQLite: {session_database['path']} "
            f"journal={session_database['journal_mode']} "
            f"synchronous={session_database['synchronous']}",
            f"Warmup: {report['warmup']['turn_count']} Turns "
            f"({report['warmup']['definition']})",
            f"Measurement: {measurement['definition']}",
            report["qualification"],
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run concurrent durable Session conversations against SQLite."
        )
    )
    parser.add_argument(
        "--database",
        type=Path,
        required=True,
        help="new Session SQLite database path (never overwritten)",
    )
    parser.add_argument(
        "--run-database",
        type=Path,
        required=True,
        help="new Run SQLite database path (never overwritten)",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        required=True,
        help="path for the environment-qualified JSON result",
    )
    args = parser.parse_args()
    try:
        report = asyncio.run(
            execute_session_workload(
                session_database=args.database,
                run_database=args.run_database,
                session_count=MEASURED_SESSIONS,
                turns_per_session=TURNS_PER_SESSION,
            )
        )
    except (SessionWorkloadValidationError, FileExistsError, ValueError) as exc:
        print(f"BENCHMARK FAILED: {exc}", file=sys.stderr)
        return 1
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(format_report(report))
    print(f"JSON evidence: {args.json_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
