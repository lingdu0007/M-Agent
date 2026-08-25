"""Durable Support Agent 旗舰示例一键运行入口。

编排五个子进程完成确定性验收，退出码反映 acceptance 成败：

1. ``worker.py ticket-update-and-crash``：第一进程在 IDEMPOTENT
   ticket update 的外部效果后、checkpoint 前硬崩溃；
2. ``worker.py resume-after-ticket-update``：第二进程自动重放该安全
   Step，外部 ticket-update ledger 仍只包含一次更新；
3. ``worker.py notify-and-crash``：第一进程执行全部步骤并在
   NON_IDEMPOTENT 通知效果之后、Tool Step checkpoint 之前硬崩溃；
4. ``worker.py resume-and-confirm``：第二进程恢复（WAITING，无重复
   通知）并提交 CONFIRM_STEP，Run 到达 SUCCEEDED；
5. ``eval.py``：确定性 Eval 读取公开产物与 fake external evidence，
   把独立报告写入 report/ 目录。

用法：:

    python examples/durable_support_agent/run_acceptance.py [--workdir DIR]

- 未传 ``--workdir`` 时使用新的临时目录（保留并打印，便于检查产物）；
- 全部 acceptance 检查通过时退出码为 0，场景或验收失败为 1；无效命令行
  输入（包括非空 ``--workdir``）由 ``argparse`` 退出 2。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))

#: 崩溃子进程的约定退出码（worker.py 的 _CRASH_EXIT_CODE）。
_CRASH_EXIT_CODE = 17

#: 恢复进程在 stdout 上打印的状态行（与 worker.py 一致）。
_STATUS_LINE = "STATUS="


def _run_script(args: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
    """用当前解释器运行示例内的脚本（继承父进程环境）。

    ``timeout`` 防止任一子进程挂起时无限阻塞（与既有子进程测试一致）。
    """
    return subprocess.run(
        [sys.executable, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the Durable Support Agent flagship acceptance "
        "scenario offline and deterministically."
    )
    parser.add_argument(
        "--workdir",
        default=None,
        help="artifacts directory (db, journals, logs, evidence, report); "
        "defaults to a fresh temporary directory",
    )
    args = parser.parse_args()
    if args.workdir is None:
        workdir = tempfile.mkdtemp(prefix="durable-support-")
    else:
        workdir = os.path.abspath(args.workdir)
        if os.path.exists(workdir):
            if not os.path.isdir(workdir) or os.listdir(workdir):
                parser.error("--workdir must be an empty directory")
        else:
            os.makedirs(workdir)

    db_path = os.path.join(workdir, "run.sqlite")
    notify_journal = os.path.join(workdir, "notify.journal")
    ticket_update_journal = os.path.join(workdir, "ticket-update.journal")
    logs_dir = os.path.join(workdir, "logs")
    ticket_replay_db = os.path.join(workdir, "ticket-update-replay.sqlite")
    ticket_replay_notify = os.path.join(workdir, "ticket-update-replay-notify.journal")
    ticket_replay_journal = os.path.join(workdir, "ticket-update-replay.journal")
    ticket_replay_logs = os.path.join(workdir, "ticket-update-replay-logs")
    evidence_path = os.path.join(workdir, "recovery_evidence.json")
    report_dir = os.path.join(workdir, "report")

    print(f"artifacts: {workdir}")
    print("phase 1/5: first process — ticket update then crash before checkpoint")
    proc = _run_script(
        [
            os.path.join(_HERE, "worker.py"),
            ticket_replay_db,
            ticket_replay_notify,
            ticket_replay_journal,
            ticket_replay_logs,
            "ticket-update-and-crash",
        ],
        cwd=_HERE,
    )
    ticket_replay_run_id: str | None = None
    for line in proc.stdout.splitlines():
        if line.startswith("RUN_ID="):
            ticket_replay_run_id = line.split("=", 1)[1].strip()
    if proc.returncode != _CRASH_EXIT_CODE or not ticket_replay_run_id:
        print(proc.stdout)
        print(proc.stderr, file=sys.stderr)
        print(
            "FAIL: ticket-update process expected crash exit code "
            f"{_CRASH_EXIT_CODE} with RUN_ID, got {proc.returncode}",
            file=sys.stderr,
        )
        return 1
    print(
        "  ticket update crashed with exit code "
        f"{_CRASH_EXIT_CODE}; run_id={ticket_replay_run_id}"
    )

    print("phase 2/5: second process — replay idempotent ticket update")
    proc = _run_script(
        [
            os.path.join(_HERE, "worker.py"),
            ticket_replay_db,
            ticket_replay_notify,
            ticket_replay_journal,
            ticket_replay_logs,
            "resume-after-ticket-update",
            ticket_replay_run_id,
        ],
        cwd=_HERE,
    )
    if proc.returncode != 0 or "STATUS=SUCCEEDED" not in proc.stdout:
        print(proc.stdout)
        print(proc.stderr, file=sys.stderr)
        print(
            "FAIL: ticket-update recovery did not reach SUCCEEDED",
            file=sys.stderr,
        )
        return 1

    print("phase 3/5: first process — notify then crash before checkpoint")
    proc = _run_script(
        [
            os.path.join(_HERE, "worker.py"),
            db_path,
            notify_journal,
            ticket_update_journal,
            logs_dir,
            "notify-and-crash",
        ],
        cwd=_HERE,
    )
    run_id: str | None = None
    for line in proc.stdout.splitlines():
        if line.startswith("RUN_ID="):
            run_id = line.split("=", 1)[1].strip()
    if proc.returncode != _CRASH_EXIT_CODE or not run_id:
        print(proc.stdout)
        print(proc.stderr, file=sys.stderr)
        print(
            f"FAIL: first process expected crash exit code "
            f"{_CRASH_EXIT_CODE} with RUN_ID, got {proc.returncode}",
            file=sys.stderr,
        )
        return 1
    print(f"  crashed with exit code {_CRASH_EXIT_CODE}; run_id={run_id}")

    print("phase 4/5: second process — resume into WAITING, then CONFIRM_STEP")
    proc = _run_script(
        [
            os.path.join(_HERE, "worker.py"),
            db_path,
            notify_journal,
            ticket_update_journal,
            logs_dir,
            "resume-and-confirm",
            run_id,
        ],
        cwd=_HERE,
    )
    status = None
    for line in proc.stdout.splitlines():
        if line.startswith(_STATUS_LINE):
            status = line.split("=", 1)[1].strip()
    if proc.returncode != 0 or status != "SUCCEEDED":
        print(proc.stdout)
        print(proc.stderr, file=sys.stderr)
        print(
            f"FAIL: second process expected SUCCEEDED, got "
            f"returncode={proc.returncode} status={status!r}",
            file=sys.stderr,
        )
        return 1
    print(f"  terminal status: {status}")

    print("phase 5/5: deterministic eval (Runtime Companion, offline)")
    proc = _run_script(
        [
            os.path.join(_HERE, "eval.py"),
            db_path,
            notify_journal,
            ticket_update_journal,
            logs_dir,
            evidence_path,
            report_dir,
        ],
        cwd=_HERE,
    )
    print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)
    if proc.returncode != 0:
        print("FAIL: deterministic evaluation did not pass", file=sys.stderr)
        return 1

    print(f"ACCEPTANCE PASSED — report: {os.path.join(report_dir, 'report.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
