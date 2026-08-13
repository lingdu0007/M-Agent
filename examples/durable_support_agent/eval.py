"""Durable Support Agent 确定性 Eval（Ticket 11 / ADR 0029，离线可重复）。

Eval 是 **Runtime Companion**：它不参与 Runner 核心循环，只通过公开
产物（``Runner.inspect_run`` 等只读查询路径读取权威 RunStore）与 fake
external evidence（notify / ticket-update journal、model_request.log、
provider.log、recovery_evidence.json）验证旗舰场景的确定性验收，报告
写入 **RunStore 之外的独立目录**。

验收检查（对照 Ticket 11 Acceptance criteria）：

1. ``context_items_checkpointed``：ticket 与 policy Context Item 以带
   身份 / 内容 / 来源 / 元数据的结构保留在 Context Step checkpoint；
2. ``context_items_delivered_as_data``：两个 Context Item 的完整
   item_id/content/source/metadata 以数据身份出现在模型每次请求输入中
   （ADR 0017，非 Agent Instruction）；
3. ``context_provider_not_refetched``：Context Provider 只被调用一次
   （恢复复用 checkpoint，不重新查询外部数据源）；
4. ``tool_effects_declared``：order_lookup=READ_ONLY、ticket_update=
   IDEMPOTENT、notify=NON_IDEMPOTENT（ADR 0007 显式声明）；
5. ``step_trajectory``：Step 顺序为 CONTEXT, MODEL, TOOL(order_lookup),
   MODEL, TOOL(ticket_update), MODEL, TOOL(notify), MODEL，每个 Step 均有
   唯一 Attempt identity，checkpoint identity 不复用，且 ticket-update
   ledger 恰好一行；
6. ``ticket_update_idempotent_recovery``：ticket update 在外部效果后、
   checkpoint 前崩溃；第二进程自动重放，保留原始与恢复 Attempt，外部
   ticket-update ledger 仍恰好一条；
7. ``uncertain_effect_recorded``：存在且仅存在一个 UNCERTAIN 失败
   Attempt（error_code=``effect_unconfirmed``）——崩溃发生在通知之后、
   checkpoint 之前；
8. ``notification_once``：外部 notify journal 恰好一行（全流程只产生
   一次通知）；
9. ``waiting_and_resolution``：恢复证据显示 WAITING +
   ``UNCERTAIN_NON_IDEMPOTENT`` 与全部合法 resolution actions；
   CONFIRM_STEP 复用同一 step_id 把确认结果写成 Tool checkpoint，原始
   uncertain Attempt 与确认 Attempt identity 明确不同；
10. ``terminal_succeeded``：Run 最终状态为 SUCCEEDED；
11. ``final_structured_result``：最终结构化结果含预期 ticket / order /
    ticket_update / notification 字段（普通模型输出，无 Output Repair）。

退出码：全部通过为 0，任一失败为 1（供一键运行反映 acceptance 成败）。

用法：:

    python eval.py <db> <notify_journal> <ticket_update_journal> \\
        <logs_dir> <evidence_path> <report_dir>
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from m_agent import (
    ALLOWED_FOR_UNCERTAIN_NON_IDEMPOTENT,
    ERROR_EFFECT_UNCONFIRMED,
    REASON_UNCERTAIN_NON_IDEMPOTENT,
    ContextItem,
    DefinitionRegistry,
    FailureClassification,
    PlaintextPayloadCodec,
    RunNotFoundError,
    RunStatus,
    Runner,
    SQLiteRunStore,
    StepStatus,
    StepType,
    ToolEffect,
    deserialize_tool_outcome,
)

from support_agent import (
    CONFIRM_RESULT,
    ORDER_ID,
    POLICY_CONTEXT_ITEM_ID,
    SUPPORT_CONTEXT_ITEMS,
    TICKET_CONTEXT_ITEM_ID,
    TICKET_ID,
    journal_count,
)

#: 期望的 Step 类型轨迹（对照 Ticket 11 AC 4）。
EXPECTED_TRAJECTORY = [
    "CONTEXT",
    "MODEL",
    "TOOL",
    "MODEL",
    "TOOL",
    "MODEL",
    "TOOL",
    "MODEL",
]

#: 期望的 Tool Step 顺序（外部证据 + 权威 checkpoint 双重验证）。
EXPECTED_TOOL_ORDER = ["order_lookup", "ticket_update", "notify"]

#: 期望的工具效果声明（ADR 0007）。
EXPECTED_TOOL_EFFECTS = {
    "order_lookup": ToolEffect.READ_ONLY,
    "ticket_update": ToolEffect.IDEMPOTENT,
    "notify": ToolEffect.NON_IDEMPOTENT,
}

#: 期望的 Context Item 标识（注入顺序，ADR 0016）。
EXPECTED_CONTEXT_ITEM_IDS = [TICKET_CONTEXT_ITEM_ID, POLICY_CONTEXT_ITEM_ID]


class Check:
    """一次确定性验收检查的结果（报告中的最小单元）。"""

    def __init__(self, name: str, detail: str, passed: bool) -> None:
        self.name = name
        self.detail = detail
        self.passed = passed


def _read_lines(path: str) -> list[str]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [line.rstrip("\n") for line in fh if line.strip()]


async def _check_ticket_update_recovery(db_path: str) -> Check:
    """Verify the independent IDEMPOTENT crash/replay evidence.

    This reads only the secondary SQLite RunStore and the external ledger that
    `run_acceptance.py` created.  Neither artifact contributes state to the
    flagship notification Run or to the Runner core loop.
    """
    workdir = os.path.dirname(db_path)
    replay_db = os.path.join(workdir, "ticket-update-replay.sqlite")
    replay_journal = os.path.join(workdir, "ticket-update-replay.journal")
    evidence_path = os.path.join(
        workdir, "ticket_update_recovery_evidence.json"
    )
    if not os.path.exists(replay_db):
        return Check(
            "ticket_update_idempotent_recovery",
            f"missing ticket-update replay RunStore at {replay_db}",
            False,
        )
    if not os.path.exists(evidence_path):
        return Check(
            "ticket_update_idempotent_recovery",
            f"missing ticket-update recovery evidence at {evidence_path}",
            False,
        )
    try:
        with open(evidence_path, encoding="utf-8") as fh:
            evidence = json.load(fh)
        replay_run_id = evidence["run_id"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        return Check(
            "ticket_update_idempotent_recovery",
            f"invalid ticket-update recovery evidence: {type(exc).__name__}",
            False,
        )

    ledger = _read_lines(replay_journal)
    try:
        ledger_entry = json.loads(ledger[0]) if len(ledger) == 1 else None
    except json.JSONDecodeError:
        ledger_entry = None
    ledger_ok = (
        isinstance(ledger_entry, dict)
        and ledger_entry.get("ticket_id") == TICKET_ID
        and bool(ledger_entry.get("idempotency_key"))
        and ledger_entry.get("result")
        == f"ticket {TICKET_ID} updated with verified status"
    )

    store = SQLiteRunStore(replay_db, payload_codec=PlaintextPayloadCodec())
    try:
        inspection = await Runner(
            registry=DefinitionRegistry(), store=store
        ).inspect_run(replay_run_id)
    except (RunNotFoundError, OSError, ValueError) as exc:
        return Check(
            "ticket_update_idempotent_recovery",
            f"ticket-update replay Run unavailable: {type(exc).__name__}",
            False,
        )
    finally:
        store.close()

    ticket_checkpoint = next(
        (
            checkpoint
            for checkpoint in inspection.checkpoints
            if checkpoint.step_type is StepType.TOOL
            and deserialize_tool_outcome(checkpoint.output).tool_name
            == "ticket_update"
        ),
        None,
    )
    attempts = (
        [
            attempt
            for attempt in inspection.attempts
            if attempt.step_id == ticket_checkpoint.step_id
        ]
        if ticket_checkpoint is not None
        else []
    )
    original = next(
        (attempt for attempt in attempts if attempt.status is StepStatus.FAILED),
        None,
    )
    recovered = next(
        (
            attempt
            for attempt in attempts
            if attempt.status is StepStatus.SUCCEEDED
        ),
        None,
    )
    attempts_ok = (
        len(attempts) == 2
        and original is not None
        and recovered is not None
        and original.classification is FailureClassification.UNCERTAIN
        and original.error_code == ERROR_EFFECT_UNCONFIRMED
        and recovered.attempt_id != original.attempt_id
        and ticket_checkpoint is not None
        and ticket_checkpoint.attempt_id == recovered.attempt_id
    )
    passed = (
        evidence.get("phase") == "ticket_update_replay"
        and evidence.get("resumed_status") == RunStatus.SUCCEEDED.value
        and evidence.get("ticket_update_effects") == 1
        and inspection.run.status is RunStatus.SUCCEEDED
        and len(ledger) == 1
        and ledger_ok
        and attempts_ok
    )
    return Check(
        "ticket_update_idempotent_recovery",
        f"external_updates={len(ledger)} attempts={len(attempts)} "
        f"resumed_status={inspection.run.status.value}",
        passed,
    )


def _make_report(
    checks: list[Check],
    run_id: str | None,
    notification_count: int | None = None,
    final_status: str | None = None,
    final_result: dict | None = None,
) -> dict:
    """构造统一形状的 Eval 报告（早期失败路径也走这里，保证
    ``write_report`` 总能完整写出 report.json 与 report.txt）。"""
    return {
        "scenario": "durable-support-agent",
        "run_id": run_id,
        "notification_count": notification_count,
        "final_status": final_status,
        "final_result": final_result,
        "checks": [
            {"name": c.name, "passed": c.passed, "detail": c.detail}
            for c in checks
        ],
        "passed": all(c.passed for c in checks),
    }


async def evaluate(
    db_path: str,
    notify_journal: str,
    ticket_update_journal: str,
    logs_dir: str,
    evidence_path: str,
    report_dir: str,
) -> tuple[list[Check], dict]:
    """执行全部确定性检查；返回 (检查列表, 报告数据)。"""
    checks: list[Check] = []

    # run_id 只来自公开产物：恢复证据文件（第二进程用公开 Runner 写入）。
    if not os.path.exists(evidence_path):
        checks.append(
            Check(
                "run_present",
                f"missing recovery evidence at {evidence_path}",
                False,
            )
        )
        return checks, _make_report(checks, None)
    with open(evidence_path, encoding="utf-8") as fh:
        evidence = json.load(fh)
    run_id = evidence.get("run_id")
    if not run_id:
        checks.append(
            Check(
                "run_present",
                "recovery evidence carries no run_id",
                False,
            )
        )
        return checks, _make_report(checks, None)

    if not os.path.exists(db_path):
        checks.append(
            Check(
                "run_present",
                f"missing RunStore at {db_path}",
                False,
            )
        )
        return checks, _make_report(checks, run_id)

    store = SQLiteRunStore(db_path, payload_codec=PlaintextPayloadCodec())
    try:
        runner = Runner(registry=DefinitionRegistry(), store=store)
        # 公开只读查询路径：读取权威 Run / Step / Attempt / Checkpoint。
        try:
            inspection = await runner.inspect_run(run_id)
        except RunNotFoundError as exc:
            checks.append(
                Check(
                    "run_present",
                    f"run {run_id} not found in RunStore: {exc}",
                    False,
                )
            )
            return checks, _make_report(checks, run_id)
        run = inspection.run
        steps = inspection.steps
        attempts = inspection.attempts
        checkpoints = inspection.checkpoints

        # -- 1. Context Items checkpointed（AC 2） --------------------
        expected_context_items = list(SUPPORT_CONTEXT_ITEMS)
        checkpointed_context_items: list[ContextItem] = []
        context_ckpts = [
            c for c in checkpoints if c.step_type is StepType.CONTEXT
        ]
        if len(context_ckpts) != 1:
            checks.append(
                Check(
                    "context_items_checkpointed",
                    f"expected 1 CONTEXT checkpoint, found {len(context_ckpts)}",
                    False,
                )
            )
        else:
            try:
                checkpointed_context_items = [
                    ContextItem.model_validate(obj)
                    for obj in json.loads(context_ckpts[0].output)
                ]
            except (json.JSONDecodeError, TypeError, ValueError):
                checkpointed_context_items = []
            item_ids = [i.item_id for i in checkpointed_context_items]
            ok = checkpointed_context_items == expected_context_items
            checks.append(
                Check(
                    "context_items_checkpointed",
                    "CONTEXT checkpoint retains item_id/content/source/"
                    "metadata: " + json.dumps(item_ids, ensure_ascii=False),
                    ok,
                )
            )

        # -- 2. Context Items delivered as data（AC 2 / ADR 0017） ----
        model_log = _read_lines(os.path.join(logs_dir, "model_request.log"))
        try:
            requests = [json.loads(line) for line in model_log]
            logged_context_items = [
                [
                    ContextItem.model_validate(item)
                    for item in request["context_items"]
                ]
                for request in requests
            ]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            requests = []
            logged_context_items = []
        all_requests_carry_context = (
            bool(requests)
            and checkpointed_context_items == expected_context_items
            and all(
                items == expected_context_items
                for items in logged_context_items
            )
        )
        checks.append(
            Check(
                "context_items_delivered_as_data",
                f"model received full context provenance in all "
                f"{len(requests)} requests: "
                + json.dumps(
                    [
                        [item.item_id for item in items]
                        for items in logged_context_items
                    ],
                    ensure_ascii=False,
                ),
                all_requests_carry_context,
            )
        )

        # -- 3. Provider not refetched on resume（AC 6 / Ticket 04） --
        provider_lines = _read_lines(os.path.join(logs_dir, "provider.log"))
        checks.append(
            Check(
                "context_provider_not_refetched",
                f"provider invoked {len(provider_lines)} time(s); recovery "
                "must reuse the checkpointed Context Items",
                len(provider_lines) == 1,
            )
        )

        # -- 4. Tool effects declared（AC 3 / ADR 0007） --------------
        snapshot = run.snapshot
        if snapshot is None:
            checks.append(
                Check(
                    "tool_effects_declared",
                    "run has no frozen snapshot",
                    False,
                )
            )
        else:
            declared = {d.name: d.effect for d in snapshot.tool_declarations}
            checks.append(
                Check(
                    "tool_effects_declared",
                    "snapshot declares "
                    + json.dumps(
                        {k: v.value for k, v in sorted(declared.items())},
                        ensure_ascii=False,
                    ),
                    all(
                        declared.get(name) is effect
                        for name, effect in EXPECTED_TOOL_EFFECTS.items()
                    ),
                )
            )

        # -- 5. Step trajectory with distinct attempts（AC 4） --------
        trajectory = [s.step_type.value for s in steps]
        trajectory_ok = trajectory == EXPECTED_TRAJECTORY
        tool_ckpts = [
            deserialize_tool_outcome(c.output)
            for c in checkpoints
            if c.step_type is StepType.TOOL
        ]
        tool_order = [o.tool_name for o in tool_ckpts]
        tool_order_ok = tool_order == EXPECTED_TOOL_ORDER
        checkpoint_attempt_ids = [c.attempt_id for c in checkpoints]
        distinct_checkpoint_attempts_ok = len(
            set(checkpoint_attempt_ids)
        ) == len(checkpoint_attempt_ids)
        attempt_ids = [a.attempt_id for a in attempts]
        distinct_attempts_ok = len(set(attempt_ids)) == len(attempt_ids)
        step_ids_with_attempts = {a.step_id for a in attempts}
        every_step_has_attempt_ok = all(
            step.step_id in step_ids_with_attempts for step in steps
        )
        # 幂等更新也只发生一次（崩溃 / 恢复未重放已确认步骤）。
        ticket_updates = journal_count(ticket_update_journal)
        checks.append(
            Check(
                "step_trajectory",
                f"steps={trajectory} tool_checkpoints={tool_order} "
                f"distinct_checkpoint_attempts="
                f"{distinct_checkpoint_attempts_ok} "
                f"distinct_attempts={distinct_attempts_ok} "
                f"every_step_has_attempt={every_step_has_attempt_ok} "
                f"ticket_update_journal={ticket_updates}",
                trajectory_ok
                and tool_order_ok
                and distinct_checkpoint_attempts_ok
                and distinct_attempts_ok
                and every_step_has_attempt_ok
                and ticket_updates == 1,
            )
        )

        # -- 6. IDEMPOTENT crash/replay keeps one external update --------
        checks.append(await _check_ticket_update_recovery(db_path))

        # -- 7. Uncertain non-idempotent effect recorded（AC 5/9） -----
        # ERROR_EFFECT_UNCONFIRMED 是 Tool Step 专属的机器可读错误标识
        # （Ticket 07：恢复时未确认的 NON_IDEMPOTENT 调用）。
        uncertain = [
            a for a in attempts if a.error_code == ERROR_EFFECT_UNCONFIRMED
        ]
        uncertain_ok = len(uncertain) == 1 and (
            uncertain[0].classification is not None
            and uncertain[0].classification is FailureClassification.UNCERTAIN
        )
        checks.append(
            Check(
                "uncertain_effect_recorded",
                "failed attempt with error_code="
                f"{ERROR_EFFECT_UNCONFIRMED}: "
                f"{len(uncertain)} found; classification="
                + json.dumps(
                    [
                        (
                            a.classification.value
                            if a.classification is not None
                            else None
                        )
                        for a in uncertain
                    ]
                ),
                uncertain_ok,
            )
        )

        # -- 8. Notification happened exactly once（AC 5/7/9） ---------
        notify_count = journal_count(notify_journal)
        checks.append(
            Check(
                "notification_once",
                f"external notify journal has {notify_count} line(s); "
                "crash must occur after exactly one notification effect",
                notify_count == 1,
            )
        )

        # -- 9. WAITING + CONFIRM_STEP resolution（AC 6/7/9） ---------
        allowed = evidence.get("allowed_actions", [])
        evidence_ok = (
            evidence.get("resumed_status") == RunStatus.WAITING.value
            and evidence.get("waiting_reason")
            == REASON_UNCERTAIN_NON_IDEMPOTENT
            and set(allowed)
            == {a.value for a in ALLOWED_FOR_UNCERTAIN_NON_IDEMPOTENT}
        )
        confirm_ok = False
        notification_attempts = []
        attempt_identities_distinct = False
        waiting_step_id = evidence.get("waiting_step_id")
        if (
            uncertain_ok
            and waiting_step_id == uncertain[0].step_id
        ):
            notification_attempts = [
                attempt
                for attempt in attempts
                if attempt.step_id == waiting_step_id
            ]
            confirm_ckpt = next(
                (
                    c
                    for c in checkpoints
                    if c.step_id == waiting_step_id
                    and c.step_type is StepType.TOOL
                ),
                None,
            )
            if confirm_ckpt is not None:
                outcome = deserialize_tool_outcome(confirm_ckpt.output)
                confirmed_attempt = next(
                    (
                        attempt
                        for attempt in notification_attempts
                        if attempt.attempt_id == confirm_ckpt.attempt_id
                        and attempt.status is StepStatus.SUCCEEDED
                    ),
                    None,
                )
                attempt_identities_distinct = (
                    len(notification_attempts) == 2
                    and confirmed_attempt is not None
                    and uncertain[0].attempt_id
                    != confirmed_attempt.attempt_id
                )
                confirm_ok = (
                    outcome.result == CONFIRM_RESULT
                    and attempt_identities_distinct
                )
        checks.append(
            Check(
                "waiting_and_resolution",
                f"resumed_status={evidence.get('resumed_status')} "
                f"waiting_reason={evidence.get('waiting_reason')} "
                f"allowed={allowed} "
                f"notification_attempts={len(notification_attempts)} "
                f"attempt_identities_distinct={attempt_identities_distinct} "
                f"confirm_result_applied={confirm_ok}",
                evidence_ok and confirm_ok,
            )
        )

        # -- 10. Terminal SUCCEEDED（AC 7/9） --------------------------
        checks.append(
            Check(
                "terminal_succeeded",
                f"run status={run.status.value} output={run.output!r}",
                run.status is RunStatus.SUCCEEDED,
            )
        )

        # -- 11. Final structured result（AC 8/9） ---------------------
        final_result: dict | None = None
        if run.output is not None:
            try:
                final_result = json.loads(run.output)
            except json.JSONDecodeError:
                final_result = None
        final_ok = False
        if final_result is not None:
            final_ok = (
                final_result.get("ticket_id") == TICKET_ID
                and final_result.get("status") == "resolved"
                and final_result.get("order_lookup")
                == f"order {ORDER_ID} status=shipped"
                and final_result.get("ticket_update")
                == f"ticket {TICKET_ID} updated with verified status"
                and final_result.get("notification") == CONFIRM_RESULT
                and final_result.get("notification_acknowledged_by")
                == "application"
            )
        checks.append(
            Check(
                "final_structured_result",
                json.dumps(final_result, ensure_ascii=False)
                if final_result is not None
                else "run output is not a JSON object",
                final_ok,
            )
        )

        report = _make_report(
            checks,
            run_id,
            notification_count=notify_count,
            final_status=run.status.value,
            final_result=final_result,
        )
        return checks, report
    finally:
        store.close()


def write_report(report_dir: str, report: dict) -> None:
    """把 Eval 报告写入 RunStore 之外的独立目录（ADR 0029）。"""
    os.makedirs(report_dir, exist_ok=True)
    with open(
        os.path.join(report_dir, "report.json"), "w", encoding="utf-8"
    ) as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    lines = [
        "Durable Support Agent — deterministic evaluation report",
        f"run_id={report['run_id']}",
        f"notification_count={report['notification_count']}",
        f"final_status={report['final_status']}",
        f"passed={'YES' if report['passed'] else 'NO'}",
        "",
    ]
    for c in report["checks"]:
        lines.append(
            f"[{'PASS' if c['passed'] else 'FAIL'}] {c['name']}: {c['detail']}"
        )
    with open(
        os.path.join(report_dir, "report.txt"), "w", encoding="utf-8"
    ) as fh:
        fh.write("\n".join(lines) + "\n")


def main() -> int:
    if len(sys.argv) != 7:
        print(
            "usage: python eval.py <db> <notify_journal> "
            "<ticket_update_journal> <logs_dir> <evidence_path> <report_dir>",
            file=sys.stderr,
        )
        return 2
    db_path, notify_journal, ticket_update_journal = (
        sys.argv[1],
        sys.argv[2],
        sys.argv[3],
    )
    logs_dir, evidence_path, report_dir = sys.argv[4], sys.argv[5], sys.argv[6]

    checks, report = asyncio.run(
        evaluate(
            db_path,
            notify_journal,
            ticket_update_journal,
            logs_dir,
            evidence_path,
            report_dir,
        )
    )
    write_report(report_dir, report)
    for c in checks:
        print(f"[{'PASS' if c.passed else 'FAIL'}] {c.name}: {c.detail}")
    print(
        f"summary: {sum(c.passed for c in checks)}/{len(checks)} checks "
        f"passed; report written to {os.path.abspath(report_dir)}"
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
