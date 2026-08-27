"""Durable Support Agent 旗舰示例的确定性验收测试。

通过**真实子进程**运行示例的一键入口（``run_acceptance.py``）与
确定性 Eval（``eval.py``），断言：

- 一键运行退出码反映 acceptance 成败（全部检查通过 = 0）；
- Eval 报告独立于 RunStore 写入 report/ 目录，且内容覆盖 context
  provenance、tool trajectory、一次通知、WAITING 转换、resolution、
  终态与最终结构化结果；
- Eval 真的检测副作用次数：通知 journal 出现第二行时
  ``notification_once`` 失败、退出码为 1；
- Eval 对缺失证据（找不到 Run）明确失败而非崩溃。

示例完全离线：全部适配器是确定性 fake，子进程不访问任何网络。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_EXAMPLE_DIR = os.path.abspath(
    os.path.join(_HERE, "..", "examples", "durable_support_agent")
)
_RUN_ACCEPTANCE = os.path.join(_EXAMPLE_DIR, "run_acceptance.py")
_EVAL = os.path.join(_EXAMPLE_DIR, "eval.py")
_WORKER = os.path.join(_EXAMPLE_DIR, "worker.py")

if _EXAMPLE_DIR not in sys.path:
    sys.path.insert(0, _EXAMPLE_DIR)

from m_agent.runtime import (
    AgentDefinition,
    DefinitionRegistry,
    Runner,
    RunStatus,
)
from m_agent.adapters import (
    DeterministicModelAdapter,
    PlaintextPayloadCodec,
    SQLiteRunStore,
)
from m_agent import (
    AgentDefinition,
    DefinitionRegistry,
    Runner,
    RunStatus,
)
from support_agent import TicketContextProvider

_EXPECTED_CHECK_NAMES = [
    "context_items_checkpointed",
    "context_items_delivered_as_data",
    "context_provider_not_refetched",
    "tool_effects_declared",
    "step_trajectory",
    "ticket_update_idempotent_recovery",
    "uncertain_effect_recorded",
    "notification_once",
    "waiting_and_resolution",
    "terminal_succeeded",
    "final_structured_result",
]


def _run(args: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )


def _run_acceptance(workdir: str) -> subprocess.CompletedProcess[str]:
    return _run(
        [_RUN_ACCEPTANCE, "--workdir", workdir], cwd=_EXAMPLE_DIR
    )


def _load_report(workdir: str) -> dict:
    with open(
        os.path.join(workdir, "report", "report.json"), encoding="utf-8"
    ) as fh:
        return json.load(fh)


class DurableSupportAgentExampleTests(unittest.TestCase):
    def test_flagship_acceptance_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = _run_acceptance(tmp)
            self.assertEqual(
                proc.returncode,
                0,
                msg=f"run_acceptance failed:\n{proc.stdout}\n{proc.stderr}",
            )
            report = _load_report(tmp)
            self.assertTrue(report["passed"], report)
            self.assertEqual(report["notification_count"], 1)
            self.assertEqual(report["final_status"], "SUCCEEDED")
            self.assertEqual(
                [c["name"] for c in report["checks"]],
                _EXPECTED_CHECK_NAMES,
            )
            self.assertTrue(
                all(c["passed"] for c in report["checks"]),
                [c for c in report["checks"] if not c["passed"]],
            )
            resolution = next(
                c
                for c in report["checks"]
                if c["name"] == "waiting_and_resolution"
            )
            self.assertIn("notification_attempts=2", resolution["detail"])
            self.assertIn("attempt_identities_distinct=True", resolution["detail"])
            final = report["final_result"]
            self.assertEqual(final["ticket_id"], "T-1024")
            self.assertEqual(final["status"], "resolved")
            self.assertEqual(
                final["notification"], "notification-confirmed-by-app"
            )
            with open(
                os.path.join(tmp, "logs", "model_request.log"),
                encoding="utf-8",
            ) as fh:
                model_requests = [json.loads(line) for line in fh]
            self.assertTrue(model_requests)
            self.assertTrue(
                all(
                    all(
                        {"item_id", "content", "source", "metadata"}
                        <= set(item)
                        for item in request["context_items"]
                    )
                    for request in model_requests
                )
            )
            # 报告必须位于 RunStore 之外（独立 report/ 目录）。
            self.assertTrue(
                os.path.exists(os.path.join(tmp, "report", "report.txt"))
            )
            self.assertNotEqual(
                os.path.abspath(os.path.join(tmp, "report")),
                os.path.abspath(tmp),
            )

    def test_acceptance_rejects_a_nonempty_workdir(self) -> None:
        """A rerun must not silently reuse previous external effects."""
        with tempfile.TemporaryDirectory() as tmp:
            first = _run_acceptance(tmp)
            self.assertEqual(first.returncode, 0, first.stderr)
            repeated = _run_acceptance(tmp)
            self.assertEqual(repeated.returncode, 2, repeated.stdout)
            self.assertIn("--workdir must be an empty directory", repeated.stderr)

    def test_eval_fails_on_duplicate_notification(self) -> None:
        """Eval 必须真正检测副作用次数：两次通知时退出码为 1。"""
        with tempfile.TemporaryDirectory() as tmp:
            first = _run_acceptance(tmp)
            self.assertEqual(first.returncode, 0, first.stdout)
            # 篡改外部通知证据：追加第二行，模拟重复通知。
            with open(
                os.path.join(tmp, "notify.journal"), "a", encoding="utf-8"
            ) as fh:
                fh.write("notify:2\n")
            proc = _run(
                [
                    _EVAL,
                    os.path.join(tmp, "run.sqlite"),
                    os.path.join(tmp, "notify.journal"),
                    os.path.join(tmp, "ticket-update.journal"),
                    os.path.join(tmp, "logs"),
                    os.path.join(tmp, "recovery_evidence.json"),
                    os.path.join(tmp, "report-dup"),
                ],
                cwd=_EXAMPLE_DIR,
            )
            self.assertNotEqual(proc.returncode, 0, proc.stdout)
            self.assertIn("notification_once", proc.stdout)
            with open(
                os.path.join(tmp, "report-dup", "report.json"),
                encoding="utf-8",
            ) as fh:
                report = json.load(fh)
            notification_check = next(
                c
                for c in report["checks"]
                if c["name"] == "notification_once"
            )
            self.assertFalse(notification_check["passed"])
            self.assertFalse(report["passed"])

    def test_eval_fails_when_model_context_provenance_changes(self) -> None:
        """Matching item IDs alone cannot prove model input provenance."""
        with tempfile.TemporaryDirectory() as tmp:
            first = _run_acceptance(tmp)
            self.assertEqual(first.returncode, 0, first.stdout)
            model_log = os.path.join(tmp, "logs", "model_request.log")
            with open(model_log, encoding="utf-8") as fh:
                requests = [json.loads(line) for line in fh]
            for request in requests:
                request["context_items"][0]["source"] = "unexpected-source"
            with open(model_log, "w", encoding="utf-8") as fh:
                for request in requests:
                    fh.write(json.dumps(request) + "\n")
            proc = _run(
                [
                    _EVAL,
                    os.path.join(tmp, "run.sqlite"),
                    os.path.join(tmp, "notify.journal"),
                    os.path.join(tmp, "ticket-update.journal"),
                    os.path.join(tmp, "logs"),
                    os.path.join(tmp, "recovery_evidence.json"),
                    os.path.join(tmp, "report-context-provenance"),
                ],
                cwd=_EXAMPLE_DIR,
            )
            self.assertNotEqual(proc.returncode, 0, proc.stdout)
            with open(
                os.path.join(
                    tmp, "report-context-provenance", "report.json"
                ),
                encoding="utf-8",
            ) as fh:
                report = json.load(fh)
            context_check = next(
                c
                for c in report["checks"]
                if c["name"] == "context_items_delivered_as_data"
            )
            self.assertFalse(context_check["passed"])

    def test_eval_fails_on_duplicate_ticket_update(self) -> None:
        """A second external ticket update invalidates the acceptance report."""
        with tempfile.TemporaryDirectory() as tmp:
            first = _run_acceptance(tmp)
            self.assertEqual(first.returncode, 0, first.stdout)
            with open(
                os.path.join(tmp, "ticket-update.journal"), "a", encoding="utf-8"
            ) as fh:
                fh.write('{"idempotency_key":"duplicate","result":"bad"}\n')
            proc = _run(
                [
                    _EVAL,
                    os.path.join(tmp, "run.sqlite"),
                    os.path.join(tmp, "notify.journal"),
                    os.path.join(tmp, "ticket-update.journal"),
                    os.path.join(tmp, "logs"),
                    os.path.join(tmp, "recovery_evidence.json"),
                    os.path.join(tmp, "report-duplicate-ticket"),
                ],
                cwd=_EXAMPLE_DIR,
            )
            self.assertNotEqual(proc.returncode, 0, proc.stdout)
            with open(
                os.path.join(
                    tmp, "report-duplicate-ticket", "report.json"
                ),
                encoding="utf-8",
            ) as fh:
                report = json.load(fh)
            trajectory = next(
                c for c in report["checks"] if c["name"] == "step_trajectory"
            )
            self.assertFalse(trajectory["passed"])
            self.assertFalse(report["passed"])

    def test_eval_fails_on_waiting_terminal_state(self) -> None:
        """A public resume without CONFIRM_STEP cannot pass acceptance."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "run.sqlite")
            notify_journal = os.path.join(tmp, "notify.journal")
            ticket_journal = os.path.join(tmp, "ticket-update.journal")
            logs_dir = os.path.join(tmp, "logs")
            crashed = _run(
                [
                    _WORKER,
                    db_path,
                    notify_journal,
                    ticket_journal,
                    logs_dir,
                    "notify-and-crash",
                ],
                cwd=_EXAMPLE_DIR,
            )
            self.assertEqual(crashed.returncode, 17, crashed.stderr)
            run_id = next(
                line.split("=", 1)[1]
                for line in crashed.stdout.splitlines()
                if line.startswith("RUN_ID=")
            )
            waiting = _run(
                [
                    _WORKER,
                    db_path,
                    notify_journal,
                    ticket_journal,
                    logs_dir,
                    "resume-and-wait",
                    run_id,
                ],
                cwd=_EXAMPLE_DIR,
            )
            self.assertEqual(waiting.returncode, 0, waiting.stderr)
            self.assertIn("STATUS=WAITING", waiting.stdout)
            proc = _run(
                [
                    _EVAL,
                    db_path,
                    notify_journal,
                    ticket_journal,
                    logs_dir,
                    os.path.join(tmp, "recovery_evidence.json"),
                    os.path.join(tmp, "report-waiting"),
                ],
                cwd=_EXAMPLE_DIR,
            )
            self.assertNotEqual(proc.returncode, 0, proc.stdout)
            with open(
                os.path.join(tmp, "report-waiting", "report.json"),
                encoding="utf-8",
            ) as fh:
                report = json.load(fh)
            terminal = next(
                c
                for c in report["checks"]
                if c["name"] == "terminal_succeeded"
            )
            self.assertFalse(terminal["passed"])

    def test_eval_fails_on_wrong_step_trajectory(self) -> None:
        """A successful but non-flagship public Runner flow cannot pass."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "wrong-trajectory.sqlite")
            logs_dir = os.path.join(tmp, "logs")
            os.makedirs(logs_dir)

            async def create_wrong_trajectory_run() -> str:
                store = SQLiteRunStore(
                    db_path, payload_codec=PlaintextPayloadCodec()
                )
                try:
                    registry = DefinitionRegistry()
                    registry.register(
                        AgentDefinition.for_adapter(
                            definition_id="wrong-trajectory",
                            version="1.0",
                            instructions="Return a deterministic answer.",
                            model_adapter=DeterministicModelAdapter(
                                responses=("not the support trajectory",)
                            ),
                            context_provider=TicketContextProvider(logs_dir),
                        )
                    )
                    runner = Runner(registry=registry, store=store)
                    run = await runner.create_run(
                        "wrong-trajectory", "1.0", "test input"
                    )
                    terminal = await runner.start_run(run.run_id)
                    self.assertIs(terminal.status, RunStatus.SUCCEEDED)
                    return run.run_id
                finally:
                    store.close()

            run_id = asyncio.run(create_wrong_trajectory_run())
            evidence_path = os.path.join(tmp, "recovery_evidence.json")
            with open(evidence_path, "w", encoding="utf-8") as fh:
                json.dump({"run_id": run_id}, fh)
            proc = _run(
                [
                    _EVAL,
                    db_path,
                    os.path.join(tmp, "notify.journal"),
                    os.path.join(tmp, "ticket-update.journal"),
                    logs_dir,
                    evidence_path,
                    os.path.join(tmp, "report-wrong-trajectory"),
                ],
                cwd=_EXAMPLE_DIR,
            )
            self.assertNotEqual(proc.returncode, 0, proc.stdout)
            with open(
                os.path.join(
                    tmp, "report-wrong-trajectory", "report.json"
                ),
                encoding="utf-8",
            ) as fh:
                report = json.load(fh)
            trajectory = next(
                c for c in report["checks"] if c["name"] == "step_trajectory"
            )
            self.assertFalse(trajectory["passed"])
            self.assertFalse(report["passed"])

    def test_eval_fails_cleanly_on_missing_run(self) -> None:
        """缺失恢复证据（找不到 Run）时 Eval 明确失败而非崩溃。"""
        with tempfile.TemporaryDirectory() as tmp:
            proc = _run(
                [
                    _EVAL,
                    os.path.join(tmp, "missing.sqlite"),
                    os.path.join(tmp, "notify.journal"),
                    os.path.join(tmp, "ticket-update.journal"),
                    os.path.join(tmp, "logs"),
                    os.path.join(tmp, "recovery_evidence.json"),
                    os.path.join(tmp, "report"),
                ],
                cwd=_EXAMPLE_DIR,
            )
            self.assertNotEqual(proc.returncode, 0, proc.stdout)
            with open(
                os.path.join(tmp, "report", "report.json"),
                encoding="utf-8",
            ) as fh:
                report = json.load(fh)
            self.assertFalse(report["passed"])

    def test_eval_does_not_create_a_missing_runstore(self) -> None:
        """Eval is read-only when external evidence names a missing RunStore."""
        with tempfile.TemporaryDirectory() as tmp:
            evidence_path = os.path.join(tmp, "recovery_evidence.json")
            with open(evidence_path, "w", encoding="utf-8") as fh:
                json.dump({"run_id": "missing-run"}, fh)
            db_path = os.path.join(tmp, "missing.sqlite")
            proc = _run(
                [
                    _EVAL,
                    db_path,
                    os.path.join(tmp, "notify.journal"),
                    os.path.join(tmp, "ticket-update.journal"),
                    os.path.join(tmp, "logs"),
                    evidence_path,
                    os.path.join(tmp, "report"),
                ],
                cwd=_EXAMPLE_DIR,
            )
            self.assertNotEqual(proc.returncode, 0, proc.stdout)
            self.assertFalse(os.path.exists(db_path))


if __name__ == "__main__":
    unittest.main()
