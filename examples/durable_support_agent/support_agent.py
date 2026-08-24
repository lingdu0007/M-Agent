"""Durable Support Agent 旗舰示例的共享组件（Ticket 11）。

本模块把 M-Agent 已有 runtime 能力组装成可执行验收场景所需的
确定性组件，全部离线、可重复：

- :class:`TicketContextProvider`：确定性 Context Provider，返回带
  ``item_id`` / ``content`` / ``source`` / ``metadata`` 的 ticket 与
  policy Context Item（ADR 0014 / 0016 / 0017，作为数据交付给模型）；
- :class:`SupportModel`：确定性 fake Model Adapter，按 tool_outcomes
  演进依次请求 ``order_lookup`` -> ``ticket_update`` -> ``notify``，
  收齐结果后给出最终结构化结果（JSON，无 Output Repair）；
- :class:`OrderLookupTool`：``READ_ONLY`` 订单查询；
- :class:`TicketUpdateTool`：``IDEMPOTENT`` 工单更新（独立 journal
  记录幂等更新次数，作为外部证据）；
- :class:`NotifyTool`：``NON_IDEMPOTENT`` 通知（独立 journal 记录
  通知次数，作为外部副作用证据——与 RunStore 分离，重复执行可观测）。

外部证据文件与 RunStore 分离（PRD「Side-effect evidence」）：notify /
ticket-update 的效果只写入各自的 journal 文件，跨进程后仍可计数；
模型请求与 Context Provider 调用也各写一个日志文件，供确定性 Eval
读取。**本模块不包含 RAG / Session / Workflow / MultiAgent / LLM
judge / UI / hosted service**——只组合公开 Runner 与 SQLiteRunStore
能力。

场景：ticket/policy Context Items -> READ_ONLY order lookup ->
IDEMPOTENT ticket update -> NON_IDEMPOTENT notification -> 崩溃 ->
第二进程恢复 WAITING -> 应用 CONFIRM_STEP -> SUCCEEDED。
"""

from __future__ import annotations

import json
import os
from hashlib import sha256
from datetime import timedelta
from pathlib import Path

import fcntl

from m_agent.runtime import (
    AgentDefinition,
    ContextItem,
    ContextRequest,
    DefinitionRegistry,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    RetryPolicy,
    ToolCall,
    ToolEffect,
    ToolOutcome,
    ToolRequest,
)
from m_agent.adapters import (
    DeterministicContextProvider,
    DeterministicModelAdapter,
    DeterministicTool,
    FakeClock,
    PlaintextPayloadCodec,
    SQLiteRunStore,
)
from m_agent import (
    AgentDefinition,
    DefinitionRegistry,
)
from m_agent.runtime import ModelRequirements, ToolCallingMode

#: 本示例使用的定义标识与版本（不可变、版本化，ADR 0022）。
DEFINITION_ID = "durable-support-agent"
DEFINITION_VERSION = "1.0"

#: 应用在第二进程对等待中的通知 Tool Step 提交的确认结果
#: （ADR 0008 / Ticket 07：CONFIRM_STEP 由应用显式提供）。
CONFIRM_RESULT = "notification-confirmed-by-app"

#: 模型所需的 tool calling 能力（ADR 0030：注册时校验，无静默降级）。
_TOOL_CALLING = ModelCapabilities(tool_calling=ToolCallingMode.NATIVE)

#: 场景中固定的工单 / 订单 / 通知参数（确定性演示数据）。
TICKET_ID = "T-1024"
ORDER_ID = "ORD-7788"
CUSTOMER_EMAIL = "cust@example.com"

#: 预期注入的 Context Item 标识（Eval 用它验证 context provenance）。
TICKET_CONTEXT_ITEM_ID = "ticket-T-1024"
POLICY_CONTEXT_ITEM_ID = "policy-refund-v2024.3"

#: 旗舰场景交付给模型的完整外部 Context Items。Provider checkpoint 与
#: 模型输入日志都使用同一不可变样本，Eval 可因此比较完整 provenance。
SUPPORT_CONTEXT_ITEMS = (
    ContextItem(
        item_id=TICKET_CONTEXT_ITEM_ID,
        content=(
            f"Customer reports order {ORDER_ID} was not delivered; "
            "ticket requires a resolution and a customer notification."
        ),
        source="fake-ticket-db",
        metadata={"ticket_id": TICKET_ID, "priority": "high"},
    ),
    ContextItem(
        item_id=POLICY_CONTEXT_ITEM_ID,
        content=(
            "Support policy: confirm the order status first, update "
            "the ticket with the verified status, then notify the "
            "customer about the outcome."
        ),
        source="fake-policy-db",
        metadata={"policy_version": "2024.3"},
    ),
)


def journal_count(path: str) -> int:
    """外部 journal 文件的行数（= 该副作用累计发生次数）。

    journal 是独立于 RunStore 的外部证据：重复执行即可确定性观测
    （Ticket 07 AC 2 / PRD「Side-effect evidence」）。
    """
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as fh:
        return sum(1 for _ in fh)


def _append_line(path: str, line: str) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _ticket_update_identity(ticket_id: str, note: str) -> str:
    """Return the stable external identity for one ticket-update operation.

    The identity intentionally excludes Runner and Step Attempt identifiers:
    an interrupted IDEMPOTENT Step is allowed to have a new Attempt on
    recovery, while the external operation remains the same ticket/note
    update.
    """
    operation = json.dumps(
        {"ticket_id": ticket_id, "note": note},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return sha256(operation.encode("utf-8")).hexdigest()


def _apply_ticket_update_once(
    journal_path: str, ticket_id: str, note: str
) -> str:
    """Apply an idempotent external ticket update exactly once per identity.

    The JSONL ledger is deliberately outside RunStore.  An exclusive OS file
    lock covers read-before-write across processes, and the first effect is
    flushed before the ToolOutcome is returned.  Recovery may therefore replay
    the Tool Step while observing the already-applied external operation.
    """
    identity = _ticket_update_identity(ticket_id, note)
    result = f"ticket {ticket_id} updated with verified status"
    Path(journal_path).parent.mkdir(parents=True, exist_ok=True)
    with open(journal_path, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.seek(0)
            for line in fh:
                if not line.strip():
                    continue
                entry = json.loads(line)
                if entry.get("idempotency_key") == identity:
                    return entry["result"]
            entry = {
                "idempotency_key": identity,
                "note": note,
                "result": result,
                "ticket_id": ticket_id,
            }
            fh.seek(0, os.SEEK_END)
            fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True))
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
            return result
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class TicketContextProvider(DeterministicContextProvider):
    """确定性 ticket + policy 上下文（ADR 0014 / 0016 / 0017）。

    返回带稳定标识与溯源的 Context Item；每次调用向 provider 日志追加
    一行，跨进程调用次数因此可观测（恢复必须复用 checkpoint，不再
    查询外部数据源，Ticket 04）。
    """

    deterministic: bool = True

    def __init__(self, logs_dir: str) -> None:
        super().__init__()
        self._log_path = os.path.join(logs_dir, "provider.log")
        self._items = SUPPORT_CONTEXT_ITEMS

    async def provide(self, request: ContextRequest) -> list[ContextItem]:
        self.call_count += 1
        # 跨进程调用序号以日志行数 + 1 为准（恢复进程的实例 call_count
        # 会重置，不能作为跨进程证据）。
        n = journal_count(self._log_path) + 1
        _append_line(self._log_path, f"provide:{n}")
        return list(self._items)


class SupportModel(DeterministicModelAdapter):
    """确定性支持 Agent 模型：按已收工具结果演进工具请求。

    每次 generate 向 ``model_request.log`` 追加一行 JSON 证据：
    ``context_item_ids``（Context Items 以数据身份出现在模型输入，
    ADR 0017）与 ``tool_outcomes`` 摘要（工具轨迹证据）。模型本身
    不访问任何网络，响应完全由构造参数与请求决定。
    """

    deterministic: bool = True

    def __init__(self, logs_dir: str) -> None:
        super().__init__(capabilities=_TOOL_CALLING)
        self._log_path = os.path.join(logs_dir, "model_request.log")

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        self._last_request = request
        _append_line(
            self._log_path,
            json.dumps(
                {
                    "seq": self.call_count,
                    "context_item_ids": [i.item_id for i in request.context_items],
                    "context_items": [
                        item.model_dump() for item in request.context_items
                    ],
                    "tool_outcomes": [
                        o.tool_name for o in request.tool_outcomes
                    ],
                },
                ensure_ascii=False,
            ),
        )
        by_tool = {o.tool_name: o for o in request.tool_outcomes}
        if "order_lookup" not in by_tool:
            return ModelResponse(
                tool_calls=(
                    ToolCall(
                        call_id="call-order-lookup",
                        tool_name="order_lookup",
                        arguments=json.dumps({"order_id": ORDER_ID}),
                    ),
                )
            )
        if "ticket_update" not in by_tool:
            return ModelResponse(
                tool_calls=(
                    ToolCall(
                        call_id="call-ticket-update",
                        tool_name="ticket_update",
                        arguments=json.dumps(
                            {
                                "ticket_id": TICKET_ID,
                                "note": "order status verified by lookup",
                            }
                        ),
                    ),
                )
            )
        if "notify" not in by_tool:
            return ModelResponse(
                tool_calls=(
                    ToolCall(
                        call_id="call-notify",
                        tool_name="notify",
                        arguments=json.dumps(
                            {
                                "customer_email": CUSTOMER_EMAIL,
                                "message": (
                                    f"Your order {ORDER_ID} is on the way."
                                ),
                            }
                        ),
                    ),
                )
            )
        # 三个工具结果齐备：给出确定性最终结构化结果（JSON 文本）。
        # 这是普通模型输出，不是 Output Repair（Ticket 11 AC 8）。
        result = {
            "ticket_id": TICKET_ID,
            "status": "resolved",
            "order_lookup": by_tool["order_lookup"].result,
            "ticket_update": by_tool["ticket_update"].result,
            "notification": by_tool["notify"].result,
            "notification_acknowledged_by": "application",
        }
        return ModelResponse(
            content=json.dumps(result, ensure_ascii=False, indent=2)
        )


class OrderLookupTool(DeterministicTool):
    """READ_ONLY 订单查询：不产生外部副作用，可安全重放（ADR 0007）。"""

    deterministic: bool = True

    def __init__(self) -> None:
        super().__init__(
            name="order_lookup",
            description="Look up the current status of a customer order.",
            effect=ToolEffect.READ_ONLY,
            parameters={
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
            },
        )

    async def invoke(self, request: ToolRequest) -> ToolOutcome:
        return ToolOutcome.success(
            request.call_id,
            self.name,
            result=f"order {ORDER_ID} status=shipped",
        )


class TicketUpdateTool(DeterministicTool):
    """IDEMPOTENT 工单更新：重复执行产生相同外部效果（ADR 0007）。

    外部 JSONL ledger 以 ticket/note 的稳定 identity 去重：首次调用才
    追加一条真实更新证据，恢复后的重放复用同一结果。它不属于 RunStore，
    因而真实外部效果次数能跨进程独立观察。
    """

    deterministic: bool = True

    def __init__(self, journal_path: str) -> None:
        super().__init__(
            name="ticket_update",
            description=(
                "Update the support ticket with a verified status note."
            ),
            effect=ToolEffect.IDEMPOTENT,
            parameters={
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["ticket_id", "note"],
            },
        )
        self._journal_path = journal_path

    async def invoke(self, request: ToolRequest) -> ToolOutcome:
        arguments = json.loads(request.arguments)
        ticket_id = arguments["ticket_id"]
        note = arguments["note"]
        if not isinstance(ticket_id, str) or not isinstance(note, str):
            raise ValueError("ticket_update requires string ticket_id and note")
        result = _apply_ticket_update_once(
            self._journal_path, ticket_id, note
        )
        return ToolOutcome.success(
            request.call_id,
            self.name,
            result=result,
        )


class NotifyTool(DeterministicTool):
    """NON_IDEMPOTENT 通知：重复执行会产生不同外部效果（ADR 0007）。

    每次调用向独立 journal 追加一行——通知是否发生、发生几次都由
    这个 RunStore 之外的外部证据确定性证明（Ticket 07 / 11 AC）。
    结果不确定时运行时绝不自动重放，进入 WAITING 等待应用处置。
    """

    deterministic: bool = True

    def __init__(self, journal_path: str) -> None:
        super().__init__(
            name="notify",
            description="Send a notification to the customer.",
            effect=ToolEffect.NON_IDEMPOTENT,
            parameters={
                "type": "object",
                "properties": {
                    "customer_email": {"type": "string"},
                    "message": {"type": "string"},
                },
                "required": ["customer_email", "message"],
            },
        )
        self._journal_path = journal_path

    async def invoke(self, request: ToolRequest) -> ToolOutcome:
        n = journal_count(self._journal_path) + 1
        _append_line(self._journal_path, f"notify:{n}")
        return ToolOutcome.success(
            request.call_id, self.name, result=f"notification-{n}"
        )


def build_registry(
    notify_journal: str, ticket_update_journal: str, logs_dir: str
) -> DefinitionRegistry:
    """按精确 id + version 注册旗舰场景的 Agent Definition。

    全部适配器都是确定性 fake（``deterministic=True``），示例默认
    离线且可重复（Ticket 11 AC 1）。
    """
    os.makedirs(logs_dir, exist_ok=True)
    registry = DefinitionRegistry()
    registry.register(
        AgentDefinition.for_adapter(
            definition_id=DEFINITION_ID,
            version=DEFINITION_VERSION,
            instructions=(
                "You are a durable support agent. Use the provided ticket "
                "and policy context as data. Look up the order, update the "
                "ticket, notify the customer, then return the structured "
                "result."
            ),
            model_requirements=ModelRequirements(capabilities=_TOOL_CALLING),
            model_adapter=SupportModel(logs_dir),
            context_provider=TicketContextProvider(logs_dir),
            tools=(
                OrderLookupTool(),
                TicketUpdateTool(ticket_update_journal),
                NotifyTool(notify_journal),
            ),
            # Ticket 06 requires an explicit frozen bound before recovery may
            # replay an effect-safe Step.  TicketUpdateTool's external ledger
            # makes its second attempt idempotent; NON_IDEMPOTENT notify still
            # enters WAITING on an uncertain crash regardless of this policy.
            retry_policy=RetryPolicy(max_attempts=2),
        )
    )
    return registry


async def open_after_crash(
    db_path: str, run_id: str
) -> tuple[SQLiteRunStore, FakeClock]:
    """重开崩溃进程遗留的数据库，并把时钟推进到崩溃租约过期之后。

    崩溃进程（``os._exit``）的租约仍持久化在数据库中（ADR 0013）；
    第二进程必须等租约过期才能接管。通过公开 ``get_run`` 读取持久化
    的 ``lease_expires_at``，用 :class:`FakeClock` 确定性推进，避免
    真实 sleep（与 tests/fixtures/notification_worker.py 同一模式）。
    """
    probe = SQLiteRunStore(db_path, payload_codec=PlaintextPayloadCodec())
    try:
        crashed = await probe.get_run(run_id)
        assert crashed is not None, f"run {run_id} not found"
        assert crashed.lease_expires_at is not None, (
            "crashed run must carry a persisted lease"
        )
        expires = crashed.lease_expires_at
    finally:
        probe.close()
    clock = FakeClock(start=expires + timedelta(seconds=1))
    store = SQLiteRunStore(
        db_path, payload_codec=PlaintextPayloadCodec(), clock=clock
    )
    return store, clock


def ensure_logs_dir(logs_dir: str) -> str:
    """确保日志目录存在并返回该目录（供 worker / eval 复用）。"""
    Path(logs_dir).mkdir(parents=True, exist_ok=True)
    return logs_dir
