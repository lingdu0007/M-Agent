"""Durable Support Agent 跨进程执行脚本（Ticket 11 示例，仅离线使用）。

两个进程通过**公开 Runner API**（``create_run`` / ``start_run`` /
``resume_run`` / ``resolve_run``）与真实 ``SQLiteRunStore`` 推进同一个
Run，绝不手工拼装 Run 记录或直接跳状态：

- ``mode=notify-and-crash``：第一进程创建并启动 Run。确定性模型依次
  请求 ``order_lookup`` -> ``ticket_update`` -> ``notify``；``notify``
  把外部效果写入独立 journal 后成功返回，进程在 Tool Step checkpoint
  提交**之前**硬崩溃（``os._exit``，退出码 17，不运行 finally）。
  stdout 打印 ``RUN_ID=<run_id>``。
- ``mode=ticket-update-and-crash``：同样通过公开 Runner 执行，但在
  IDEMPOTENT ``ticket_update`` 的首次外部效果后、checkpoint 前硬崩溃。
  第二进程的自动重放必须复用外部 idempotency identity，不能产生第二次
  ticket 更新。
- ``mode=resume-and-confirm <run_id> <confirm_result>``：第二进程重开
  同一 SQLite 数据库，时钟越过崩溃遗留租约后 ``resume_run``；Run 必须
  进入 WAITING（reason ``UNCERTAIN_NON_IDEMPOTENT``，无重复通知）。
  恢复证据写入 ``recovery_evidence.json``（Eval 的 fake external
  evidence），然后应用通过 ``resolve_run`` 提交 ``CONFIRM_STEP`` 提供
  确认结果，Run 到达 SUCCEEDED。stdout 打印 ``STATUS=<status>``。
- ``mode=resume-after-ticket-update <run_id>``：第二进程重开 ticket
  update 崩溃遗留的 SQLite，调用公开 ``resume_run`` 自动重放并完成。
- ``mode=resume-and-wait <run_id>``：第二进程只调用公开 ``resume_run``，
  留下真实 WAITING 记录供 Eval 的错误终态负向验证。

用法：:

    python worker.py <db> <notify_journal> <ticket_update_journal> \\
        <logs_dir> notify-and-crash
    python worker.py <db> <notify_journal> <ticket_update_journal> \\
        <logs_dir> resume-and-confirm <run_id> <confirm_result>
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from support_agent import (
    CONFIRM_RESULT,
    DEFINITION_ID,
    DEFINITION_VERSION,
    build_registry,
    journal_count,
    open_after_crash,
)

from m_agent.runtime import (
    REASON_UNCERTAIN_NON_IDEMPOTENT,
    CrashPoint,
    RunResolution,
    RunStatus,
    Runner,
    allowed_resolutions,
)
from m_agent.adapters import (
    PlaintextPayloadCodec,
    SQLiteRunStore,
)
from m_agent import (
    RunStatus,
    Runner,
)

_CRASH_EXIT_CODE = 17


def _crash_after_notification(
    db_path: str, notify_journal: str
) -> "CrashPoint":
    """构造崩溃注入回调（仅示例的确定性演示，生产不使用 crash_hook）。

    ``BEFORE_TOOL_CHECKPOINT`` 对每个工具都会触发；只有外部 notify
    journal 已有 1 行（= 通知效果已发生）时才硬崩溃，保证恰好一次
    通知效果发生在崩溃之前（Ticket 11 AC 5）。
    """

    def crash_hook(point: CrashPoint, run_id: str) -> None:
        if (
            point is CrashPoint.BEFORE_TOOL_CHECKPOINT
            and journal_count(notify_journal) == 1
        ):
            print(f"RUN_ID={run_id}", flush=True)
            os._exit(_CRASH_EXIT_CODE)  # noqa: PLR1722 - 硬崩溃模拟

    return crash_hook


def _crash_after_ticket_update(
    ticket_update_journal: str, notify_journal: str
) -> "CrashPoint":
    """Inject the IDEMPOTENT crash window without reaching notification."""

    def crash_hook(point: CrashPoint, run_id: str) -> None:
        if (
            point is CrashPoint.BEFORE_TOOL_CHECKPOINT
            and journal_count(ticket_update_journal) == 1
            and journal_count(notify_journal) == 0
        ):
            print(f"RUN_ID={run_id}", flush=True)
            os._exit(_CRASH_EXIT_CODE)  # noqa: PLR1722 - deterministic crash

    return crash_hook


def main() -> None:
    db_path = sys.argv[1]
    notify_journal = sys.argv[2]
    ticket_update_journal = sys.argv[3]
    logs_dir = sys.argv[4]
    mode = sys.argv[5]
    run_id = sys.argv[6] if len(sys.argv) > 6 else None
    confirm_result = sys.argv[7] if len(sys.argv) > 7 else CONFIRM_RESULT

    async def run() -> None:
        if mode in {"notify-and-crash", "ticket-update-and-crash"}:
            registry = build_registry(
                notify_journal, ticket_update_journal, logs_dir
            )
            store = SQLiteRunStore(
                db_path, payload_codec=PlaintextPayloadCodec()
            )
            runner = Runner(
                registry=registry,
                store=store,
                crash_hook=(
                    _crash_after_notification(db_path, notify_journal)
                    if mode == "notify-and-crash"
                    else _crash_after_ticket_update(
                        ticket_update_journal, notify_journal
                    )
                ),
            )
            created = await runner.create_run(
                DEFINITION_ID, DEFINITION_VERSION, input="resolve ticket T-1024"
            )
            await runner.start_run(created.run_id)
            # 未崩溃（journal 计数异常）时不打印 RUN_ID，让编排失败。
            raise SystemExit(
                f"{mode} finished without crashing; "
                "crash hook did not fire"
            )

        if mode in {
            "resume-and-confirm",
            "resume-after-ticket-update",
            "resume-and-wait",
        }:
            assert run_id is not None, f"{mode} requires run_id"
            registry = build_registry(
                notify_journal, ticket_update_journal, logs_dir
            )
            store, _ = await open_after_crash(db_path, run_id)
            try:
                runner = Runner(registry=registry, store=store)
                if mode == "resume-after-ticket-update":
                    terminal = await runner.resume_run(run_id)
                    if terminal.status is not RunStatus.SUCCEEDED:
                        raise SystemExit(
                            "expected IDEMPOTENT replay to reach SUCCEEDED, "
                            f"got {terminal.status.value}"
                        )
                    evidence = {
                        "phase": "ticket_update_replay",
                        "run_id": run_id,
                        "resumed_status": terminal.status.value,
                        "ticket_update_effects": journal_count(
                            ticket_update_journal
                        ),
                    }
                    evidence_path = os.path.join(
                        os.path.dirname(db_path),
                        "ticket_update_recovery_evidence.json",
                    )
                    with open(evidence_path, "w", encoding="utf-8") as fh:
                        json.dump(evidence, fh, ensure_ascii=False, indent=2)
                        fh.write("\n")
                else:
                    # 第二进程恢复：必须是 WAITING，且不追加任何通知。
                    waiting = await runner.resume_run(run_id)
                    if waiting.status is not RunStatus.WAITING:
                        raise SystemExit(
                            f"expected WAITING after resume, got "
                            f"{waiting.status.value}"
                        )
                    if (
                        waiting.waiting_reason
                        != REASON_UNCERTAIN_NON_IDEMPOTENT
                    ):
                        raise SystemExit(
                            f"expected {REASON_UNCERTAIN_NON_IDEMPOTENT} "
                            f"waiting reason, got {waiting.waiting_reason!r}"
                        )
                    # WAITING 是中间态，终态后权威记录不保留 reason / step。
                    evidence = {
                        "phase": "recovery",
                        "run_id": run_id,
                        "resumed_status": waiting.status.value,
                        "waiting_reason": waiting.waiting_reason,
                        "waiting_step_id": waiting.waiting_step_id,
                        "allowed_actions": [
                            a.value for a in allowed_resolutions(waiting)
                        ],
                    }
                    evidence_path = os.path.join(
                        os.path.dirname(db_path), "recovery_evidence.json"
                    )
                    with open(evidence_path, "w", encoding="utf-8") as fh:
                        json.dump(evidence, fh, ensure_ascii=False, indent=2)
                        fh.write("\n")
                    if mode == "resume-and-wait":
                        terminal = waiting
                    else:
                        # 应用显式处置：CONFIRM_STEP 提供确认结果（ADR 0008）。
                        terminal = await runner.resolve_run(
                            run_id,
                            RunResolution.confirm_step(
                                confirm_result,
                                waiting_step_id=waiting.waiting_step_id,
                            ),
                            expected_version=waiting.version,
                        )
            finally:
                store.close()
            print(f"RUN_ID={run_id}", flush=True)
            print(f"STATUS={terminal.status.value}", flush=True)
            return

        raise SystemExit(f"unknown mode {mode!r}")

    asyncio.run(run())


if __name__ == "__main__":
    main()
