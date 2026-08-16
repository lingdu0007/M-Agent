"""异步 Runner：创建、启动、恢复、检查 Agent Run 的公开控制入口。

ADR 0009：Runner 采用 async-first 嵌入式模型，不内置后台 worker、
消息队列或独立服务。Runner 只执行调用方显式交给它的 Run
（`start_run(run_id)` / `resume_run(run_id)`），绝不隐式扫描
RunStore、排队或接管租约；进程部署与任务调度完全由上层应用负责。

Ticket 02 恢复语义（ADR 0003 / PRD）：

- 恢复是 at-least-once：已确认持久化的 Checkpoint 被复用、不重复
  调用模型；在 Checkpoint 落盘前中断的执行会在恢复时重新执行，
  因此模型调用可能发生不止一次，绝不宣称 exactly-once。
- 恢复只读取 Run Store 与精确 `definition_id + version` 解析的
  Definition（ADR 0023）；原版本不可用时 Run 进入 WAITING，reason
  为机器可读的 ``DEFINITION_UNAVAILABLE``，绝不回退到最新版本。

Ticket 03 并发控制（ADR 0013 / PRD User Stories 21-23, 44）：

- Runner 在推进非终态 Run 前先从 RunStore 排他获取 Run Lease
  （``acquire_lease``）；无有效租约（被他人持有）时抛
  :class:`LeaseNotHeldError`，绝无第二个有效推进者。
- 获取后的每一步权威写入（Step / Attempt / Checkpoint / 状态转换）
  都携带 expected version 与租约 owner，并在 Store 侧原子校验；每个
  新 Context / Model / Tool Attempt dispatch 前也重新校验版本与租约；
  租约过期被接管后，旧 owner 的任何迟到提交都会被拒绝。
- 正常到达终态后 Runner 显式释放租约；异常中断（含模拟崩溃）时
  租约保留至过期，由上层应用或后续 Runner 在过期后接管。
- 不同 Run 的租约相互独立，可并发推进；运行时没有全局执行锁、
  后台扫描、自动 takeover、queue 或 scheduler。

Ticket 07 不确定副作用处置（ADR 0008 / PRD US 13, 37-42）：

- NON_IDEMPOTENT 工具的结果不确定（工具主动抛 UNCERTAIN 失败，或
  崩溃发生在外部效果之后、Tool Step checkpoint 提交之前）时，Run
  进入 WAITING，reason 为机器可读的 ``UNCERTAIN_NON_IDEMPOTENT``，
  并记录目标 Step（``waiting_step_id``）；恢复**绝不自动重放**未确认
  的非幂等副作用。
- 上层应用通过唯一的公开入口 :meth:`resolve_run` 显式处置：
  ``RETRY_STEP``（新 Step Attempt，重新执行）、``CONFIRM_STEP(result)``
  （不执行工具，写入应用确认结果）、``FAIL_RUN``（FAILED）、
  ``CANCEL_RUN``（CANCELLED）。**模型没有任何 resolution 通道**。
- resolution 命令受状态、Run Lease（ADR 0013）与乐观版本三重约束；
  重复、过期、非法命令以显式异常失败，绝不改动权威记录。

Ticket 08 实时观察与协作式取消（ADR 0010 / 0011 / 0012）：

- Run Update 是稳定但**非权威**的实时契约：上层应用通过公开入口
  :meth:`subscribe_run` 订阅，模型流式增量携带 run_id / step_id /
  attempt_id 发布（``MODEL_DELTA``）；增量不写入 Run Store，只有
  完整模型响应才 checkpoint（``STEP_COMPLETED``）。订阅者断开或
  处理失败只移除订阅，绝不改变 Run 执行与权威状态；应用重连后必须
  读取 Run Store 重建事实，本契约不承诺持久回放。
- :meth:`cancel_run` 是公开控制面上的 Cancellation Request（ADR
  0012）：CREATED 直接安全终结为 CANCELLED；本 Runner 正在推进的
  RUNNING Run 登记协作取消请求，由推进者在**安全边界**
  （新 Step 开始前、流式 delta 之间）转 CANCELLED。取消只阻止后续
  Step 启动，**不宣称强制中断或撤销已经发出的模型 / 工具调用**——
  in-flight 调用正常完成并按需 checkpoint（外部副作用已经发生）。
  跨进程取消不在本 Ticket 范围：取消请求是进程内协作信号，跨进程的
  取消编排由上层应用自行实现（本运行时不做消息队列 / 事件总线）。
"""

from __future__ import annotations

import asyncio
import enum
import time
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import timedelta

from ._context import (
    ContextItem,
    ContextRequest,
    deserialize_context_items,
    serialize_context_items,
)
from ._definition import AgentDefinition, DefinitionRegistry, RetryPolicy
from ._errors import (
    DefinitionNotFoundError,
    IllegalRunTransitionError,
    LeaseNotHeldError,
    ModelContractViolationError,
    ResolutionNotAllowedError,
    RunNotFoundError,
    StaleRunVersionError,
)
from ._failure import ToolFailure, classify_exception
from ._model import (
    ModelAdapter,
    ModelDelta,
    ModelPurpose,
    ModelRequest,
    ModelResponse,
    StreamingMode,
    assert_model_request_compatible,
    deserialize_model_response,
    normalize_model_response,
    serialize_model_response,
)
from ._resolution import (
    ResolutionAction,
    RunResolution,
    require_allowed,
)
from ._run import RunInspection, RunRecord
from ._status import RunStatus
from ._steps import (
    FailureClassification,
    StepAttempt,
    StepCheckpoint,
    StepRecord,
    StepStatus,
    StepType,
)
from ._store import RunLease, RunStore
from ._telemetry import (
    TelemetryEvent,
    TelemetryEventType,
    TelemetrySink,
)
from ._tools import (
    Tool,
    ToolCall,
    ToolEffect,
    ToolOutcome,
    ToolRequest,
    deserialize_tool_outcome,
    serialize_tool_outcome,
)
from ._updates import RunUpdate, RunUpdateType

#: 恢复时 Definition 缺失的机器可读 WAITING reason（ADR 0023）。
REASON_DEFINITION_UNAVAILABLE = "DEFINITION_UNAVAILABLE"

#: UNCERTAIN NON_IDEMPOTENT Tool Step 进入 WAITING 的机器可读 reason
#: （ADR 0007 / Ticket 06）：运行时绝不自动重放不确定的非幂等副作用，
#: 等待上层应用通过显式 resolution（Ticket 07）处置。
REASON_UNCERTAIN_NON_IDEMPOTENT = "UNCERTAIN_NON_IDEMPOTENT"

#: 恢复时发现未确认的 NON_IDEMPOTENT 工具调用（外部效果可能已在崩溃前
#: 发生、但 checkpoint 未提交）的失败 Attempt 机器可读错误标识
#: （Ticket 07）：与工具主动抛 UNCERTAIN 时使用同一标识，保证恢复路径
#: 与执行路径产生一致的机器可读证据。
ERROR_EFFECT_UNCONFIRMED = "effect_unconfirmed"

#: Frozen Model Execution Budget exhausted before a further provider call.
ERROR_MODEL_EXECUTION_BUDGET_EXCEEDED = "MODEL_EXECUTION_BUDGET_EXCEEDED"

#: Run Snapshot 未提供唯一 Tool Effect 声明时的稳定错误标识。Runner 不得
#: 使用恢复进程当前注册的 callable 来猜测该 Run 的重试安全性。
ERROR_FROZEN_TOOL_DECLARATION_UNAVAILABLE = (
    "FROZEN_TOOL_DECLARATION_UNAVAILABLE"
)

#: 默认租约有效期（ADR 0013）。上层应用可通过 Runner 构造参数覆盖；
#: 租约过期后其他 Runner 才能接管。
DEFAULT_LEASE_TTL = timedelta(seconds=30)


class CrashPoint(str, enum.Enum):
    """确定性崩溃注入点（仅用于跨进程恢复测试）。

    测试子进程通过 ``Runner(crash_hook=...)`` 在下列边界终止进程，
    以验证从持久化状态恢复；生产使用默认 ``crash_hook=None``，
    该注入点在正常执行中不存在。
    """

    #: Context Provider 调用完成之后、任何 Step 记录落盘之前。
    #: （at-least-once：恢复时 Context Step 会重新执行。）
    BEFORE_CONTEXT_CHECKPOINT = "before_context_checkpoint"
    #: Context Step Checkpoint 已持久化之后、依赖它的 Model Step 之前。
    #: 恢复时复用 Context Items，不重新查询外部数据。
    AFTER_CONTEXT_CHECKPOINT = "after_context_checkpoint"
    #: 模型调用完成之后、任何 Step 记录落盘之前。
    BEFORE_MODEL_CHECKPOINT = "before_model_checkpoint"
    #: Model Attempt 预算已持久化、provider dispatch 尚未开始。
    AFTER_MODEL_ATTEMPT_RESERVATION = "after_model_attempt_reservation"
    #: Model Step Checkpoint 已持久化之后、Run 终态写入之前。
    AFTER_MODEL_CHECKPOINT = "after_model_checkpoint"
    #: 工具调用完成之后、任何 Tool Step 记录落盘之前。
    #: （at-least-once：恢复时该 Tool Step 会重新执行。）
    BEFORE_TOOL_CHECKPOINT = "before_tool_checkpoint"
    #: Tool Step Checkpoint 已持久化之后、下一个步骤之前。
    #: 恢复时复用该 Tool Outcome，不重复执行外部副作用。
    AFTER_TOOL_CHECKPOINT = "after_tool_checkpoint"
    #: 失败 Attempt 已持久化之后、自动重试的下一次 dispatch 之前。
    #: 用于证明恢复时预算来自 RunStore，而不是进程内循环计数。
    AFTER_ATTEMPT_FAILED = "after_attempt_failed"


def new_id() -> str:
    return uuid.uuid4().hex


class Runner:
    """Agent Run 的异步推进组件。

    :param crash_hook: 确定性崩溃注入回调（仅测试用，默认 None）。
        签名 ``crash_hook(point, run_id)``；在 :class:`CrashPoint`
        边界被调用，回调抛出的异常会终止推进。
    :param telemetry_sink: 可选 Telemetry Sink（Ticket 09 / ADR 0035）。
        Runner 在 Run / Step / Attempt 生命周期事件上调用
        ``sink.emit(TelemetryEvent)``；事件只用于观测，不含 Run
        Payload，且 Sink 失败被隔离（捕获并继续推进），绝不覆盖或
        伪造 RunStore 权威状态。
    :param telemetry_error_callback: 可选回调，Sink 抛出的异常被
        隔离捕获后调用（便于上层观测 telemetry 自身故障）；回调自身
        抛出的异常同样被吞掉，不影响 Run 执行。
    """

    def __init__(
        self,
        registry: DefinitionRegistry,
        store: RunStore,
        crash_hook: Callable[[CrashPoint, str], None] | None = None,
        *,
        owner: str | None = None,
        lease_ttl: timedelta = DEFAULT_LEASE_TTL,
        telemetry_sink: TelemetrySink | None = None,
        telemetry_error_callback: (
            Callable[[Exception], None] | None
        ) = None,
    ) -> None:
        self._registry = registry
        self._store = store
        self._crash_hook = crash_hook
        #: 本 Runner 实例的租约 owner 标识（ADR 0013）。默认自动生成，
        #: 因此每个 Runner 实例天然拥有不同的 owner，可被持久化、验证。
        self._owner = owner if owner is not None else f"runner-{new_id()}"
        self._lease_ttl = lease_ttl
        #: 每个 Run 的 Run Update 订阅队列集合（Ticket 08 / ADR 0010）。
        #: 进程内订阅：队列只在本 Runner 发布，订阅者断开即移除，
        #: 绝不改变 Run 执行与权威状态。
        self._subscribers: dict[str, set[asyncio.Queue[RunUpdate]]] = {}
        #: 每个 Run 的协作取消请求（Ticket 08 / ADR 0012）。``cancel_run``
        #: 在推进者持有租约时登记事件；推进者在安全边界检查并转 CANCELLED。
        #: 取消是进程内协作信号，跨进程取消由上层应用自行编排。
        self._cancel_events: dict[str, asyncio.Event] = {}
        #: 本 Runner 实例当前正在推进（start / resume / resolution 继续）
        #: 的 Run 集合。用于区分"同 owner 的活跃推进者"与"无推进者的
        #: 遗留 RUNNING"：``cancel_run`` 对活跃推进者只登记协作取消，
        #: 绝不因同 owner 续约而直接终结并释放租约。
        self._active_advancers: set[str] = set()
        #: 可选 Telemetry Sink（Ticket 09 / ADR 0035）。事件只用于观测，
        #: 默认不含 Run Payload；Sink 失败被 :meth:`_emit_telemetry` 隔离。
        self._telemetry_sink = telemetry_sink
        #: Sink 异常的可观测回调（默认 None）：隔离捕获后调用，绝不
        #: 覆盖或伪造 RunStore 状态。
        self._telemetry_error_callback = telemetry_error_callback
        #: Step Attempt 计时起点（``(run_id, step_id, attempt_id) ->
        #: time.monotonic()``），用于计算 STEP_COMPLETED / ATTEMPT_FAILED
        #: 的 ``duration_ms``。起点缺失（恢复 / CONFIRM_STEP 等未发布
        #: STEP_STARTED 的路径）时 duration 为 None。
        self._telemetry_timings: dict[tuple[str, str, str], float] = {}

    @property
    def owner(self) -> str:
        """本 Runner 的租约 owner 标识。"""
        return self._owner

    # -- 公开控制入口 -------------------------------------------------

    async def create_run(
        self, definition_id: str, version: str, input: str
    ) -> RunRecord:
        """在没有任何模型调用之前，持久化一个可检查的 CREATED 记录。

        定义在创建时即按精确 id + version 解析，缺失提前失败。
        """
        definition = self._registry.resolve(definition_id, version)
        run = RunRecord(
            run_id=new_id(),
            definition_id=definition.definition_id,
            definition_version=definition.version,
            input=input,
            status=RunStatus.CREATED,
            snapshot=definition.frozen_snapshot(),
        )
        result = await self._store.create_run(run)
        # Ticket 09：Run 生命周期从 CREATED 起即可观测（与 Run Store
        # 的权威 CREATED 记录一一对应；telemetry 不承载 payload）。
        self._emit_telemetry(
            TelemetryEvent(
                event_type=TelemetryEventType.RUN_STATUS_CHANGED,
                run_id=result.run_id,
                run_status=RunStatus.CREATED,
            )
        )
        return result

    async def start_run(self, run_id: str) -> RunRecord:
        """从 CREATED 启动指定的单个 Run 并推进至终态。

        冻结 Definition Snapshot -> 排他获取 Run Lease（ADR 0013）->
        RUNNING -> 执行模型-工具循环（Model Step；若模型请求工具则
        顺序执行 Tool Step 并 checkpoint，把 Tool Outcome 作为数据
        回到模型，直到最终响应）-> SUCCEEDED。租约被其他 Runner
        持有时抛 :class:`LeaseNotHeldError`；定义缺失时显式报错
        （创建时已解析过）。自动重试只依据冻结在 Definition Snapshot
        中的显式 Retry Policy（Ticket 06 / ADR 0025），无策略不重试。
        """
        run = await self._get_existing_run(run_id)
        if run.status is not RunStatus.CREATED:
            raise IllegalRunTransitionError(
                f"cannot start run {run_id}: already in {run.status.value}"
            )
        definition = self._registry.resolve(
            run.definition_id, run.definition_version
        )
        # 登记本实例为活跃推进者：``cancel_run`` 据此区分"同 owner 的
        # 活跃推进者"与"无推进者的遗留 RUNNING"，只对前者登记协作取消
        # （ADR 0012），绝不因同 owner 续约而直接终结并释放租约。
        self._active_advancers.add(run_id)
        try:
            lease = await self._store.acquire_lease(
                run.run_id,
                self._owner,
                self._lease_ttl,
                expected_version=run.version,
            )
            return await self._start_with_lease(run, definition, lease)
        finally:
            self._active_advancers.discard(run_id)

    async def _start_with_lease(
        self,
        run: RunRecord,
        definition: AgentDefinition,
        lease: RunLease,
    ) -> RunRecord:
        """在已持有租约的前提下从 CREATED 启动（start/resume 共用）。"""
        snapshot = run.snapshot
        if snapshot is None:
            # Backward-compatible migration for CREATED records persisted
            # before Model Bindings became a create-time requirement.
            snapshot = definition.frozen_snapshot()
        else:
            self._assert_adapter_contract_matches_snapshot(run, definition)
        running = await self._store.transition_run(
            run.run_id,
            expected_version=run.version,
            status=RunStatus.RUNNING,
            snapshot=snapshot if run.snapshot is None else None,
            lease_owner=lease.owner,
        )
        self._publish_status(run.run_id, RunStatus.RUNNING)
        return await self._execute_steps(running, definition, lease)

    async def resume_run(self, run_id: str) -> RunRecord:
        """从持久化状态恢复并推进一个非终态 Run（跨进程恢复入口）。

        - 终态 Run 拒绝恢复（IllegalRunTransitionError）；
        - 从未启动（CREATED）的 Run 走与 :meth:`start_run` 相同的路径；
        - ``UNCERTAIN_NON_IDEMPOTENT`` 等待中的 Run 幂等返回当前记录，
          只能等待应用显式 resolution；
        - ``DEFINITION_UNAVAILABLE`` 等待中的 Run 在精确旧 Definition
          仍缺失时保持 WAITING；应用重新注册精确版本后，公开 resume
          会在 lease / version 保护下回到 RUNNING 并复用既有 checkpoint；
        - RUNNING 的 Run 基于 Run Store 恢复：已确认的 Checkpoint 被
          复用、不重复调用模型；精确 Definition 缺失进入 WAITING
          （``DEFINITION_UNAVAILABLE``），绝不自动使用最新版本。

        推进前必须排他获取 Run Lease；租约被其他 Runner 持有时抛
        :class:`LeaseNotHeldError`。租约已过期（含崩溃遗留）时本方法
        会成功接管并继续推进。
        """
        run = await self._get_existing_run(run_id)
        if run.status.is_terminal:
            raise IllegalRunTransitionError(
                f"cannot resume run {run_id}: already in terminal "
                f"{run.status.value}"
            )
        if run.status is RunStatus.WAITING:
            if run.waiting_reason != REASON_DEFINITION_UNAVAILABLE:
                return run
            # 不存在精确旧版本时，DEFINITION_UNAVAILABLE 仍是稳定的
            # WAITING 状态。绝不借此回退到最新注册版本，也不获取租约
            # 或改变权威 Run 记录。
            try:
                definition = self._registry.resolve(
                    run.definition_id, run.definition_version
                )
            except DefinitionNotFoundError:
                return run
            self._assert_adapter_contract_matches_snapshot(run, definition)
            self._active_advancers.add(run_id)
            try:
                lease = await self._store.acquire_lease(
                    run.run_id,
                    self._owner,
                    self._lease_ttl,
                    expected_version=run.version,
                )
                # 合法的 WAITING -> RUNNING 转换会清除缺失定义标记；随后
                # 复用与普通恢复相同的权威 checkpoint 重建逻辑。
                running = await self._store.transition_run(
                    run.run_id,
                    expected_version=run.version,
                    status=RunStatus.RUNNING,
                    lease_owner=lease.owner,
                )
                self._publish_status(run.run_id, RunStatus.RUNNING)
                return await self._resume_running(running, lease)
            finally:
                self._active_advancers.discard(run_id)
        self._active_advancers.add(run_id)
        try:
            if run.status is not RunStatus.CREATED:
                try:
                    definition = self._registry.resolve(
                        run.definition_id, run.definition_version
                    )
                except DefinitionNotFoundError:
                    # _resume_running 保持既有的缺失定义 -> WAITING 语义。
                    pass
                else:
                    self._assert_adapter_contract_matches_snapshot(
                        run, definition
                    )
            lease = await self._store.acquire_lease(
                run.run_id,
                self._owner,
                self._lease_ttl,
                expected_version=run.version,
            )
            if run.status is RunStatus.CREATED:
                # 从未启动：与 start_run 相同路径；但按 ADR 0023，精确定义
                # 缺失时 resume 进入 WAITING 而非抛错。
                try:
                    definition = self._registry.resolve(
                        run.definition_id, run.definition_version
                    )
                except DefinitionNotFoundError:
                    return await self._enter_waiting_definition_unavailable(
                        run, lease
                    )
                return await self._start_with_lease(run, definition, lease)
            return await self._resume_running(run, lease)
        finally:
            self._active_advancers.discard(run_id)

    async def get_run(self, run_id: str) -> RunRecord:
        """读取权威 Run 记录；不存在抛 RunNotFoundError。"""
        run = await self._store.get_run(run_id)
        if run is None:
            raise RunNotFoundError(f"run {run_id} not found")
        return run

    async def inspect_run(self, run_id: str) -> RunInspection:
        """通过公开查询路径获取 Run、Step、Attempt 与 Checkpoint 快照。"""
        run = await self.get_run(run_id)
        steps = await self._store.get_steps(run_id)
        attempts = await self._store.get_attempts(run_id)
        checkpoints = await self._store.get_checkpoints(run_id)
        return RunInspection(
            run=run, steps=steps, attempts=attempts, checkpoints=checkpoints
        )

    async def subscribe_run(self, run_id: str) -> AsyncIterator[RunUpdate]:
        """订阅一个 Run 的实时 Run Update 流（Ticket 08 / ADR 0010）。

        这是唯一的 Run Update 订阅入口，面向上层应用：订阅者不需要
        访问任何内部执行对象（AC 1）。用法：

            async for update in runner.subscribe_run(run_id):
                ...  # 渲染状态、Step 进度与模型流式增量

        契约：

        - ``MODEL_DELTA`` 携带 run_id / step_id / attempt_id，增量文本
          在 ``content`` 字段（AC 2）；增量**不是 checkpoint**（AC 3）；
        - ``STEP_COMPLETED`` 表示该 Step 已完整 checkpoint（AC 4）；
        - 订阅者断开（break / 取消迭代）或处理失败只移除订阅，**不
          改变 Run 执行或权威状态**（AC 6）；
        - 流是 live-only：不重放历史事件。应用重连后必须读取 RunStore
          重建权威事实（``get_run`` / ``inspect_run``），本契约不承诺
          持久回放（AC 7 / ADR 0010）。
        """
        await self._get_existing_run(run_id)
        queue: asyncio.Queue[RunUpdate] = asyncio.Queue()
        self._subscribers.setdefault(run_id, set()).add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            # 订阅者断开 / 处理失败：只移除订阅，不影响 Run。
            self._subscribers.get(run_id, set()).discard(queue)
            if not self._subscribers.get(run_id):
                self._subscribers.pop(run_id, None)

    async def cancel_run(
        self,
        run_id: str,
        expected_version: int | None = None,
    ) -> RunRecord:
        """提交协作式 Cancellation Request（Ticket 08 / ADR 0012）。

        Cancellation Request 是"停止继续推进该 Run"的意图（CONTEXT.md），
        不是强制中断：本方法**不承诺撤销或打断已经发出的模型 / 工具
        调用**，也不把副作用误报为已撤销（AC 9）。

        各状态的语义：

        - ``CREATED``：从未执行，直接安全终结为 CANCELLED（开始前
          取消），模型 / 工具 / provider 零调用；
        - ``WAITING``：复用 :meth:`resolve_run` 的 ``CANCEL_RUN`` 处置；
        - ``RUNNING``：若本 Runner 有活跃推进者，则登记协作取消请求
          并返回当前记录——推进者在**安全边界**（新 Step 开始前、
          流式 delta 之间）把 Run 转为 CANCELLED（AC 8）。若本 Runner
          不掌握活跃推进循环，则显式拒绝；租约过期不能证明旧 owner
          已无 in-flight 外部调用，因而不能据此抢写 CANCELLED；
        - 终态：抛 :class:`IllegalRunTransitionError`（重复取消，
          AC 10）。

        ``expected_version`` 可选乐观版本约束（与 :meth:`resolve_run`
        一致）：传入时若权威版本已推进，命令抛
        :class:`StaleRunVersionError` 且不改动任何记录。

        取消请求是进程内协作信号：只有与本 Runner 共享执行循环的推进
        者能响应。跨进程取消编排由上层应用负责（本运行时不做消息队列
        或持久化事件总线）。
        """
        run = await self._get_existing_run(run_id)
        if run.status.is_terminal:
            raise IllegalRunTransitionError(
                f"cannot cancel run {run_id}: already in terminal "
                f"{run.status.value}"
            )
        if run.status is RunStatus.WAITING:
            return await self.resolve_run(
                run_id,
                RunResolution(action=ResolutionAction.CANCEL_RUN),
                expected_version=(
                    run.version
                    if expected_version is None
                    else expected_version
                ),
            )
        # CREATED 或 RUNNING：先判断本 Runner 是否有活跃推进者。
        # 同 owner 的 ``acquire_lease`` 允许续约，无法据此区分"正在推进
        # 的活跃推进者"与"无推进者的遗留 RUNNING"；因此对活跃推进者
        # 只登记协作取消（ADR 0012），绝不直接终结并释放其租约。
        if run_id in self._active_advancers:
            if (
                expected_version is not None
                and run.version != expected_version
            ):
                raise StaleRunVersionError(
                    f"stale cancellation for run {run_id}: expected version "
                    f"{expected_version}, authoritative version is "
                    f"{run.version}; run record was not mutated"
                ) from None
            self._request_cancel(run_id)
            return await self._get_existing_run(run_id)
        if run.status is RunStatus.RUNNING:
            if (
                expected_version is not None
                and run.version != expected_version
            ):
                raise StaleRunVersionError(
                    f"stale cancellation for run {run_id}: expected version "
                    f"{expected_version}, authoritative version is "
                    f"{run.version}; run record was not mutated"
                ) from None
            raise LeaseNotHeldError(
                f"cannot cancel running run {run_id} from owner "
                f"{self._owner!r}: no local safe boundary is known; "
                "lease expiry does not prove that the former owner has no "
                "in-flight external call"
            )
        try:
            lease = await self._store.acquire_lease(
                run.run_id,
                self._owner,
                self._lease_ttl,
                expected_version=run.version,
            )
        except LeaseNotHeldError:
            # 推进者持有租约但不在本实例的活跃集合（同进程其他 Runner
            # 实例或跨进程）：尽力登记协作取消；只有共享本实例执行循环
            # 的推进者能响应，跨实例/跨进程取消由上层应用编排。
            if (
                expected_version is not None
                and run.version != expected_version
            ):
                raise StaleRunVersionError(
                    f"stale cancellation for run {run_id}: expected version "
                    f"{expected_version}, authoritative version is "
                    f"{run.version}; run record was not mutated"
                ) from None
            self._request_cancel(run_id)
            return await self._get_existing_run(run_id)
        try:
            # 获取租约后重读权威记录（并发推进可能已改变状态）。
            run = await self._get_existing_run(run_id)
            if run.status.is_terminal:
                raise IllegalRunTransitionError(
                    f"cannot cancel run {run_id}: already in terminal "
                    f"{run.status.value}"
                )
            if (
                expected_version is not None
                and run.version != expected_version
            ):
                raise StaleRunVersionError(
                    f"stale cancellation for run {run_id}: expected version "
                    f"{expected_version}, authoritative version is "
                    f"{run.version}; run record was not mutated"
                )
            # CREATED 从未启动，没有 in-flight 调用，可以直接终结。
            result = await self._store.transition_run(
                run.run_id,
                expected_version=run.version,
                status=RunStatus.CANCELLED,
                lease_owner=lease.owner,
            )
            self._publish_status(run_id, RunStatus.CANCELLED)
            return result
        finally:
            await self._release_quietly(run_id, lease.owner)

    async def resolve_run(
        self,
        run_id: str,
        resolution: RunResolution,
        expected_version: int,
    ) -> RunRecord:
        """对 WAITING Agent Run 提交显式应用 resolution（ADR 0008 / Ticket 07）。

        这是**唯一**的 resolution 入口，只面向上层应用：模型契约
        （ModelRequest / ModelResponse）中不存在任何 resolution 通道，
        模型无法发出或合成 resolution 命令（Ticket 07 AC 8）。

        - ``RETRY_STEP``：显式授权重试等待中的 Tool Step——创建新的
          Step Attempt（同一 step_id，历史 Attempt 保留）并重新执行
          工具；外部副作用在应用授权前绝不发生（AC 6）。
        - ``CONFIRM_STEP(result)``：应用确认副作用已经发生；运行时
          **不执行工具**，把应用提供的 ``result`` 作为 Tool Outcome
          写入 Step / Attempt / Checkpoint 并继续推进 Run（AC 5）。
        - ``FAIL_RUN`` / ``CANCEL_RUN``：分别终结为 FAILED / CANCELLED，
          不再调用工具（AC 7）。

        命令受三重约束，失败时权威记录不被改动：

        - **状态约束**：Run 必须是 WAITING，且 action 必须属于该
          WAITING reason 的合法 actions（:func:`allowed_resolutions`）；
          终态/非 WAITING 的重复命令抛
          :class:`IllegalRunTransitionError`；
        - **租约约束**：必须持有未过期的 Run Lease（ADR 0013）；
          其他 owner 持有时抛 :class:`LeaseNotHeldError`；
        - **版本约束**：调用方必须提供其观察到的
          ``expected_version``（ADR 0013）。权威版本已推进时（并发
          resolution / 其他推进者），命令抛
          :class:`StaleRunVersionError` 且不改动任何记录——过期命令
          据此明确失败（Ticket 07 AC 9）。
        """
        run = await self._get_existing_run(run_id)
        if run.status is not RunStatus.WAITING:
            raise IllegalRunTransitionError(
                f"cannot resolve run {run_id}: not WAITING "
                f"(current {run.status.value})"
            )
        require_allowed(run, resolution.action)
        if (
            resolution.waiting_step_id is not None
            and resolution.waiting_step_id != run.waiting_step_id
        ):
            raise ResolutionNotAllowedError(
                f"resolution for run {run_id} targets waiting step "
                f"{resolution.waiting_step_id!r}, but the authoritative "
                f"waiting step is {run.waiting_step_id!r}"
            )
        if (
            resolution.action is ResolutionAction.CONFIRM_STEP
            and not resolution.result
        ):
            raise ResolutionNotAllowedError(
                f"CONFIRM_STEP for run {run_id} requires an application "
                "supplied result"
            )
        if run.version != expected_version:
            raise StaleRunVersionError(
                f"stale resolution for run {run_id}: expected version "
                f"{expected_version}, authoritative version is "
                f"{run.version}; run record was not mutated"
            )
        if resolution.action in (
            ResolutionAction.RETRY_STEP,
            ResolutionAction.CONFIRM_STEP,
        ):
            try:
                definition = self._registry.resolve(
                    run.definition_id, run.definition_version
                )
            except DefinitionNotFoundError:
                raise ResolutionNotAllowedError(
                    f"cannot {resolution.action.value} run {run_id}: "
                    f"definition {run.definition_id}@{run.definition_version} "
                    "is no longer resolvable"
                ) from None
            self._assert_adapter_contract_matches_snapshot(run, definition)
        lease = await self._store.acquire_lease(
            run.run_id,
            self._owner,
            self._lease_ttl,
            expected_version=run.version,
        )
        try:
            # 获取租约后重读权威记录：并发推进时版本/状态可能已变化，
            # 以最新记录为准，杜绝基于陈旧状态的 resolution。
            run = await self._get_existing_run(run_id)
            if run.status is not RunStatus.WAITING:
                raise IllegalRunTransitionError(
                    f"cannot resolve run {run_id}: run left WAITING before "
                    f"resolution (current {run.status.value})"
            )
            require_allowed(run, resolution.action)
            if (
                resolution.waiting_step_id is not None
                and resolution.waiting_step_id != run.waiting_step_id
            ):
                raise ResolutionNotAllowedError(
                    f"resolution for run {run_id} targets waiting step "
                    f"{resolution.waiting_step_id!r}, but the authoritative "
                    f"waiting step is {run.waiting_step_id!r}"
                )
            # 调用方预期版本（乐观控制，ADR 0013）：权威版本已推进时
            # 拒绝过期命令（AC 9），不改动任何记录。
            if run.version != expected_version:
                raise StaleRunVersionError(
                    f"stale resolution for run {run_id}: expected version "
                    f"{expected_version}, authoritative version is "
                    f"{run.version}; run record was not mutated"
                )
            if resolution.action in (
                ResolutionAction.FAIL_RUN,
                ResolutionAction.CANCEL_RUN,
            ):
                # _terminate_run 在成功时负责释放租约。
                return await self._terminate_run(
                    run,
                    lease,
                    status=(
                        RunStatus.FAILED
                        if resolution.action is ResolutionAction.FAIL_RUN
                        else RunStatus.CANCELLED
                    ),
                )
            try:
                definition = self._registry.resolve(
                    run.definition_id, run.definition_version
                )
            except DefinitionNotFoundError:
                # RETRY/CONFIRM 需要精确可执行定义；缺失时无法继续
                # （DEFINITION_UNAVAILABLE 的 WAITING 本来就不允许
                # RETRY/CONFIRM，这里防御已进入 WAITING 后定义被移除的
                # 竞态：定义在 resolution 时才解析，移除后仍不得静默继续）。
                raise ResolutionNotAllowedError(
                    f"cannot {resolution.action.value} run {run_id}: "
                    f"definition {run.definition_id}@{run.definition_version} "
                    "is no longer resolvable"
                ) from None
            self._assert_adapter_contract_matches_snapshot(run, definition)
            # 成功路径在 Run 到达终态时释放租约（与正常执行一致）。
            self._active_advancers.add(run_id)
            try:
                return await self._resolve_uncertain_continue(
                    run, definition, lease, resolution
                )
            finally:
                self._active_advancers.discard(run_id)
        except BaseException:
            # 命令失败（并发接管 / 版本过期 / 非法状态等）：归还租约，
            # 让其他 Runner 可以接管；绝不保留 active lease。
            await self._release_quietly(run_id, lease.owner)
            raise

    async def _release_quietly(self, run_id: str, owner: str) -> None:
        """尽力归还租约；租约已被接管等失败时忽略，不掩盖原始异常。"""
        try:
            current = await self._store.get_run(run_id)
            if current is None:
                return
            await self._store.release_lease(
                run_id, owner, expected_version=current.version
            )
        except (LeaseNotHeldError, RunNotFoundError, StaleRunVersionError):
            pass

    # -- Ticket 08：Run Update 发布与协作式取消辅助 -------------------

    def _publish(self, update: RunUpdate) -> None:
        """把一条 Run Update 投递给该 Run 的全部订阅者（尽力而为）。

        Run Update 是非权威实时通知（ADR 0010）：投递失败（如订阅者
        已断开但队列尚未移除）只跳过该订阅者，绝不抛错、绝不改变
        Run 执行与权威状态（AC 6）。
        """
        for queue in list(self._subscribers.get(update.run_id, ())):
            try:
                queue.put_nowait(update)
            except (asyncio.QueueFull, RuntimeError):
                # 无界队列正常不会满；防御订阅者侧关闭/异常。
                continue

    def _publish_status(self, run_id: str, status: RunStatus) -> None:
        """发布一次 Run Status 转换通知 + 对应 Telemetry 事件。

        所有权威状态转换都经过本方法（RUNNING / WAITING / 终态），
        Telemetry 的 ``RUN_STATUS_CHANGED`` 事件据此与 Run Store 记录
        一一对应；Run 到达终态时清理该 Run 的计时残留。
        """
        self._publish(
            RunUpdate(
                run_id=run_id,
                update_type=RunUpdateType.STATUS_CHANGED,
                status=status,
            )
        )
        self._emit_telemetry(
            TelemetryEvent(
                event_type=TelemetryEventType.RUN_STATUS_CHANGED,
                run_id=run_id,
                run_status=status,
            )
        )
        if status.is_terminal:
            self._clear_telemetry_timings(run_id)

    # -- Ticket 09：Telemetry 发射与错误隔离 -------------------------

    def _emit_telemetry(self, event: TelemetryEvent) -> None:
        """尽力把一条 Telemetry 事件交给 Sink，失败完全隔离。

        契约（Ticket 09 AC / ADR 0035）：

        - Sink 抛出的任何异常都被捕获并忽略（可选回调可观测），
          **绝不覆盖或伪造 RunStore 权威状态**、绝不改变 Run 推进；
        - 不重试：Telemetry 是可选、可丢弃的观测数据（ADR 0006），
          本地 JSONL 写失败不影响执行正确性；
        - 只捕获普通 Exception，CancelledError / KeyboardInterrupt /
          SystemExit 保持传播。
        """
        if self._telemetry_sink is None:
            return
        try:
            self._telemetry_sink.emit(event)
        except Exception as exc:  # 隔离：观测失败绝不波及权威状态
            if self._telemetry_error_callback is not None:
                try:
                    self._telemetry_error_callback(exc)
                except Exception:
                    pass

    def _telemetry_started(
        self, run_id: str, step_id: str, attempt_id: str
    ) -> None:
        """记录一个 Step Attempt 的计时起点（monotonic，进程内）。"""
        self._telemetry_timings[(run_id, step_id, attempt_id)] = (
            time.monotonic()
        )

    def _telemetry_duration_ms(
        self, run_id: str, step_id: str, attempt_id: str
    ) -> float | None:
        """结算一次 Step Attempt 的耗时（毫秒）。

        起点已记录（本进程内发布过 STEP_STARTED）时返回耗时并移除
        记录；起点缺失（恢复路径 / CONFIRM_STEP 未执行工具等）返回
        None，表示该 Attempt 没有可观测的执行时长。
        """
        start = self._telemetry_timings.pop(
            (run_id, step_id, attempt_id), None
        )
        if start is None:
            return None
        return (time.monotonic() - start) * 1000.0

    def _clear_telemetry_timings(self, run_id: str) -> None:
        """清理一个 Run 的全部计时残留（Run 到达终态时兜底）。"""
        stale = [
            key for key in self._telemetry_timings if key[0] == run_id
        ]
        for key in stale:
            del self._telemetry_timings[key]

    def _request_cancel(self, run_id: str) -> None:
        """登记协作取消请求（ADR 0012）。"""
        self._cancel_events.setdefault(run_id, asyncio.Event()).set()

    def _cancel_requested(self, run_id: str) -> bool:
        event = self._cancel_events.get(run_id)
        return event is not None and event.is_set()

    async def _maybe_cancel(
        self, run: RunRecord, lease: RunLease
    ) -> RunRecord | None:
        """安全边界检查：若已请求取消，把 Run 终结为 CANCELLED。

        只在**安全边界**调用（新 Step 开始前、流式 delta 之间、一次
        调用完成后）：此时没有未完成的 Step 启动决策，已发出的调用
        已经完成或由调用方负责（绝不假装强制中断）。返回最新权威
        Run 记录表示已取消；返回 None 表示继续执行。

        CANCELLED 是终态：转换后释放租约、移除取消登记并发布通知。
        """
        if not self._cancel_requested(run.run_id):
            return None
        result = await self._store.transition_run(
            run.run_id,
            expected_version=run.version,
            status=RunStatus.CANCELLED,
            lease_owner=lease.owner,
        )
        await self._store.release_lease(
            run.run_id, lease.owner, expected_version=result.version
        )
        self._cancel_events.pop(run.run_id, None)
        self._publish_status(run.run_id, RunStatus.CANCELLED)
        return result

    async def _assert_step_dispatch(
        self, run: RunRecord, lease: RunLease
    ) -> None:
        """新 Attempt 发起外部调用前校验权威版本与有效租约。"""
        await self._store.assert_lease(
            run.run_id,
            expected_version=run.version,
            owner=lease.owner,
        )

    async def _reserve_model_attempt(
        self,
        run: RunRecord,
        lease: RunLease,
        step_id: str,
        attempt_id: str,
        purpose: ModelPurpose,
    ) -> bool:
        """Persist one model dispatch reservation under the Run's lease."""
        assert run.snapshot is not None
        budget = run.snapshot.model_execution_budget
        reserved = await self._store.reserve_model_attempt(
            StepRecord(
                step_id=step_id,
                run_id=run.run_id,
                step_type=StepType.MODEL,
                status=StepStatus.RUNNING,
            ),
            StepAttempt(
                attempt_id=attempt_id,
                step_id=step_id,
                run_id=run.run_id,
                status=StepStatus.RUNNING,
                model_purpose=purpose,
            ),
            run_max_attempts=budget.run_max_attempts,
            purpose_max_attempts=budget.maximum_for(purpose),
            expected_version=run.version,
            lease_owner=lease.owner,
        )
        if reserved:
            self._maybe_crash(
                CrashPoint.AFTER_MODEL_ATTEMPT_RESERVATION, run.run_id
            )
        return reserved

    async def _stream_model(
        self,
        adapter: ModelAdapter,
        request: ModelRequest,
        run: RunRecord,
        lease: RunLease,
        step_id: str,
        attempt_id: str,
    ) -> ModelResponse | None:
        """消费流式 Model Adapter，把增量发布为 ``MODEL_DELTA`` Run Update。

        契约（ADR 0011 / Ticket 08 AC 2-4）：

        - 每个 :class:`ModelDelta` 作为带 run_id / step_id / attempt_id
          的 ``MODEL_DELTA`` 发布；增量**不写入 Run Store**；
        - 流的最后一个 :class:`ModelResponse` 返回给调用方，由调用方
          checkpoint（只有完整响应成为恢复点）；
        - 增量之间检查协作取消：若已请求取消，协作中断流（``aclose``）
          并返回 None（Run 已由 :meth:`_maybe_cancel` 转 CANCELLED），
          不把不完整的输出当作 checkpoint。
        """
        generator = adapter.stream(request)
        try:
            async for event in generator:
                if isinstance(event, ModelDelta):
                    self._publish(
                        RunUpdate(
                            run_id=run.run_id,
                            update_type=RunUpdateType.MODEL_DELTA,
                            step_id=step_id,
                            attempt_id=attempt_id,
                            step_type=StepType.MODEL,
                            content=event.content,
                        )
                    )
                    if await self._maybe_cancel(run, lease) is not None:
                        return None
                elif isinstance(event, ModelResponse):
                    return event
                else:
                    raise TypeError(
                        f"stream adapter {type(adapter).__name__} yielded "
                        f"{type(event).__name__}, expected ModelDelta or "
                        "ModelResponse"
                    )
        finally:
            # 协作中断：async generator 在挂起点收到 GeneratorExit，
            # adapter 的 finally 得以执行（尽力中断，不假装强杀）。
            try:
                await generator.aclose()
            except (RuntimeError, StopAsyncIteration):
                pass
        raise RuntimeError(
            f"stream adapter {type(adapter).__name__} ended without "
            "yielding a complete ModelResponse"
        )

    # -- Ticket 07：WAITING resolution 内部实现 ----------------------

    async def _resolve_uncertain_continue(
        self,
        run: RunRecord,
        definition: AgentDefinition,
        lease: RunLease,
        resolution: RunResolution,
    ) -> RunRecord:
        """RETRY_STEP / CONFIRM_STEP：把 WAITING Run 转回 RUNNING 并继续。

        基于 Run Store 的 checkpoint 重建执行位置（与恢复路径共用同一
        语义）：已确认的 Model / Tool checkpoint 复用，只处置等待中的
        Tool Step，然后带着全部已确认的工具结果回到模型循环直到终态。

        - CONFIRM_STEP：**不调用工具**，把应用确认结果作为 Tool
          Outcome 写入 Step / Attempt / Checkpoint；
        - RETRY_STEP：在同一 step_id 下创建**新的 Step Attempt**并
          重新执行工具（外部副作用只在此显式授权后发生）。
        """
        checkpoints = await self._store.get_checkpoints(run.run_id)
        # 目标 Step 在 WAITING 记录中机器可读（Ticket 07 AC 4）。必须在
        # 转回 RUNNING 之前保存：Store 保证 WAITING 字段只在 WAITING
        # 状态有效，转出时自动清空。
        target_step_id = run.waiting_step_id
        if target_step_id is None:
            raise ResolutionNotAllowedError(
                f"cannot {resolution.action.value} run {run.run_id}: "
                "no waiting step recorded"
            )
        context_items: list[ContextItem] = []
        if run.snapshot is not None and run.snapshot.has_context_provider:
            context_checkpoints = [
                c for c in checkpoints if c.step_type is StepType.CONTEXT
            ]
            if context_checkpoints:
                context_items = deserialize_context_items(
                    context_checkpoints[-1].output
                )
        model_checkpoints = [
            c for c in checkpoints if c.step_type is StepType.MODEL
        ]
        if not model_checkpoints:
            raise ResolutionNotAllowedError(
                f"cannot {resolution.action.value} run {run.run_id}: no "
                "model checkpoint to resume from"
            )
        last_response = deserialize_model_response(
            model_checkpoints[-1].output
        )
        confirmed = tuple(
            deserialize_tool_outcome(c.output)
            for c in checkpoints
            if c.step_type is StepType.TOOL
        )
        confirmed_ids = {o.call_id for o in confirmed}
        target_call = next(
            (
                call
                for call in last_response.tool_calls
                if call.call_id not in confirmed_ids
            ),
            None,
        )
        if target_call is None:
            raise ResolutionNotAllowedError(
                f"cannot {resolution.action.value} run {run.run_id}: no "
                "unconfirmed tool call to resolve"
            )
        # 转回 RUNNING（WAITING 字段由 Store 自动清空）。失败则保持
        # 权威记录不被改动；成功后才继续处置目标 Tool Step。
        run = await self._store.transition_run(
            run.run_id,
            expected_version=run.version,
            status=RunStatus.RUNNING,
            lease_owner=lease.owner,
        )
        self._publish_status(run.run_id, RunStatus.RUNNING)
        if resolution.action is ResolutionAction.CONFIRM_STEP:
            assert resolution.result is not None  # resolve_run 已校验
            outcome = ToolOutcome.success(
                target_call.call_id,
                target_call.tool_name,
                result=resolution.result,
            )
            await self._checkpoint_tool_outcome(
                run, lease, target_step_id, outcome
            )
            return await self._run_agent_loop(
                run,
                definition,
                lease,
                context_items=context_items,
                prior_tool_outcomes=(*confirmed, outcome),
            )
        # RETRY_STEP：同一 step_id 下创建新 Attempt 并重新执行工具。
        run, outcome = await self._run_tool_step(
            run,
            definition,
            lease,
            target_call,
            step_id=target_step_id,
            explicit_resolution=True,
        )
        if outcome is None:  # 重试又失败 -> Run FAILED 或再次 WAITING
            return run
        return await self._run_agent_loop(
            run,
            definition,
            lease,
            context_items=context_items,
            prior_tool_outcomes=(*confirmed, outcome),
        )

    async def _terminate_run(
        self, run: RunRecord, lease: RunLease, *, status: RunStatus
    ) -> RunRecord:
        """把 WAITING Run 终结为 FAILED / CANCELLED 并释放租约（AC 7）。

        只做状态转换，绝不调用工具；工具调用只可能发生在显式
        RETRY_STEP 授权之后。
        """
        result = await self._store.transition_run(
            run.run_id,
            expected_version=run.version,
            status=status,
            lease_owner=lease.owner,
        )
        await self._store.release_lease(
            run.run_id, lease.owner, expected_version=result.version
        )
        self._publish_status(run.run_id, status)
        return result

    # -- 内部推进 -----------------------------------------------------

    async def _get_existing_run(self, run_id: str) -> RunRecord:
        run = await self._store.get_run(run_id)
        if run is None:
            raise RunNotFoundError(f"run {run_id} not found")
        return run

    @staticmethod
    def _assert_adapter_contract_matches_snapshot(
        run: RunRecord, definition: AgentDefinition
    ) -> None:
        """Fail closed when an exact definition was re-registered differently.

        The complete, non-secret Contract is compared before any recovery side
        effect can be dispatched.
        """
        snapshot = run.snapshot
        if snapshot is None:
            return
        expected = snapshot.model_bindings.for_purpose(
            ModelPurpose.PRIMARY
        ).contract
        if expected != definition.model_adapter.model_contract:
            raise RuntimeError(
                f"run {run.run_id} snapshot Model Contract does not "
                "match the resolved definition; refusing to silently change "
                "recovery behavior"
            )

    async def _resume_running(
        self, run: RunRecord, lease: RunLease
    ) -> RunRecord:
        """基于 RUNNING 状态的持久化记录继续推进（已持有租约）。

        Ticket 05：恢复基于 Run Store 的 checkpoint 精确重建执行位置：

        - 已确认的 Model Step Checkpoint 复用、不重复调用模型；若该
          checkpoint 的响应已请求工具，则从缺失 Outcome 的工具调用
          继续顺序执行（已确认的 Tool Outcome 一律复用，不重复外部
          副作用），再回到模型循环，直到产生不含工具请求的最终响应。
        - 未确认的 Tool 调用按 Tool Effect 区分（Ticket 07）：READ_ONLY
          与 IDEMPOTENT 工具保持 at-least-once 重放语义（checkpoint 前
          中断 = 重新执行）；**NON_IDEMPOTENT 工具绝不自动重放**——
          checkpoint 未提交意味着外部副作用可能已在崩溃前发生，结果
          不确定，Run 进入 WAITING 等待应用显式处置（ADR 0007）。
        - 已确认的 Context Step 直接复用其 Items，不重新查询外部
          数据源（外部数据即使变化也不重写 Run 的上下文）。
        - 无任何 Model Step checkpoint 时，从 Context Step（如有）
          与模型循环重新开始（at-least-once：checkpoint 落盘前的
          步骤重新执行，绝不宣称 exactly-once）。
        """
        try:
            definition = self._registry.resolve(
                run.definition_id, run.definition_version
            )
        except DefinitionNotFoundError:
            return await self._enter_waiting_definition_unavailable(
                run, lease
            )
        self._assert_adapter_contract_matches_snapshot(run, definition)
        checkpoints = await self._store.get_checkpoints(run.run_id)
        model_checkpoints = [
            c for c in checkpoints if c.step_type is StepType.MODEL
        ]
        tool_checkpoints = [
            c for c in checkpoints if c.step_type is StepType.TOOL
        ]
        persisted_steps = await self._store.get_steps(run.run_id)
        persisted_attempts = await self._store.get_attempts(run.run_id)
        inflight_step = next(
            (
                step
                for step in reversed(persisted_steps)
                if step.step_type is StepType.TOOL
                and step.status is StepStatus.RUNNING
            ),
            None,
        )
        inflight_attempt = (
            next(
                (
                    attempt
                    for attempt in reversed(persisted_attempts)
                    if attempt.step_id == inflight_step.step_id
                    and attempt.status is StepStatus.RUNNING
                ),
                None,
            )
            if inflight_step is not None
            else None
        )
        inflight_model_step = next(
            (
                step
                for step in reversed(persisted_steps)
                if step.step_type is StepType.MODEL
                and step.status is StepStatus.RUNNING
            ),
            None,
        )
        # 是否注入外部上下文以冻结 Snapshot 的 has_context_provider 为准
        # （ADR 0022/0023：恢复行为由 Run 启动时冻结的定义决定，后续注册
        # 的同 id+version 定义不能改变恢复行为）。已确认的 Context Step
        # 直接复用其 Items，不重新查询外部数据源。
        context_items: list[ContextItem] = []
        if run.snapshot is not None and run.snapshot.has_context_provider:
            context_checkpoints = [
                c for c in checkpoints if c.step_type is StepType.CONTEXT
            ]
            if context_checkpoints:
                context_items = deserialize_context_items(
                    context_checkpoints[-1].output
                )
            else:
                # 上次执行在 Context checkpoint 落盘前中断
                # （at-least-once：Context Step 重新执行）。
                if definition.context_provider is None:
                    # 冻结快照声明了 Context Provider，但精确解析的定义
                    # 没有——定义与快照不一致，显式失败而不是静默改变
                    # 恢复行为（绝不把已声明的上下文静默降级为空）。
                    raise RuntimeError(
                        f"run {run.run_id} snapshot declares a context "
                        "provider but the resolved definition has none; "
                        "refusing to silently change recovery behavior"
                    )
                run, context_items = await self._run_context_step(
                    run, definition, lease
                )
                if run.status is not RunStatus.RUNNING:
                    return run
        if not model_checkpoints:
            # 上次执行在模型结果持久化前中断（at-least-once：重新执行
            # 模型循环）。
            return await self._run_agent_loop(
                run,
                definition,
                lease,
                context_items=context_items,
                resumed_model_step_id=(
                    inflight_model_step.step_id
                    if inflight_model_step is not None
                    else None
                ),
            )
        # 最后一个已确认的 Model Step checkpoint：解析其完整响应。
        last = model_checkpoints[-1]
        last_response = deserialize_model_response(last.output)
        if not last_response.tool_calls:
            # 最终模型响应已确认：复用，不重复调用模型，直接写入终态。
            result = await self._store.transition_run(
                run.run_id,
                expected_version=run.version,
                status=RunStatus.SUCCEEDED,
                output=last_response.content,
                lease_owner=lease.owner,
            )
            await self._store.release_lease(
                run.run_id, lease.owner, expected_version=result.version
            )
            self._publish_status(run.run_id, RunStatus.SUCCEEDED)
            return result
        # 该响应请求了工具：重建已确认的 Tool Outcomes（按 checkpoint
        # 顺序），从缺失 Outcome 的调用继续顺序执行；已确认的调用
        # 绝不重复执行外部副作用。
        prior_outcomes = tuple(
            deserialize_tool_outcome(c.output) for c in tool_checkpoints
        )
        confirmed_call_ids = {o.call_id for o in prior_outcomes}
        for call in last_response.tool_calls:
            if call.call_id in confirmed_call_ids:
                continue
            # Ticket 07：未确认的 NON_IDEMPOTENT 工具调用——外部效果可能
            # 已在崩溃前发生、但 checkpoint 未提交（结果不确定）。绝不
            # 自动重放（ADR 0007 fail-closed），进入 WAITING 等待应用
            # 显式处置；READ_ONLY / IDEMPOTENT 保持 at-least-once 重放
            # 语义（ADR 0003，副作用安全）。
            frozen_effect = self._frozen_tool_effect(run, call.tool_name)
            if frozen_effect is None:
                if inflight_step is not None:
                    return await self._enter_waiting_preserving_failure(
                        run, lease, inflight_step.step_id
                    )
                return await self._fail_missing_frozen_tool_declaration(
                    run, lease, call.tool_name
                )
            if (
                frozen_effect is ToolEffect.NON_IDEMPOTENT
                and inflight_step is not None
            ):
                return await self._enter_waiting_preserving_failure(
                    run, lease, inflight_step.step_id, inflight_attempt
                )
            if inflight_step is not None and inflight_attempt is not None:
                # The dispatch had no completed checkpoint. Preserve the
                # original attempt as an uncertain failure before the
                # at-least-once replay creates a new Attempt identity.
                await self._record_failed_attempt(
                    run,
                    lease,
                    inflight_step.step_id,
                    (
                        FailureClassification.UNCERTAIN,
                        ERROR_EFFECT_UNCONFIRMED,
                        "tool checkpoint was not committed before interruption; "
                        "the external effect may have occurred",
                    ),
                    StepType.TOOL,
                    attempt_id=inflight_attempt.attempt_id,
                )
                await self._record_failed_step(
                    run, lease, inflight_step.step_id, StepType.TOOL
                )
            run, outcome = await self._run_tool_step(
                run,
                definition,
                lease,
                call,
                step_id=(
                    inflight_step.step_id if inflight_step is not None else None
                ),
                recovery_replay=inflight_attempt is not None,
            )
            if outcome is None:  # 工具失败 -> Run FAILED
                return run
            prior_outcomes = (*prior_outcomes, outcome)
            # A recovered in-flight identity belongs only to the first
            # missing call. Later calls in the same model response have not
            # been dispatched and must receive fresh Step / Attempt IDs.
            inflight_step = None
            inflight_attempt = None
        # 带着全部已确认的 Tool Outcomes 回到模型循环，直到最终响应。
        return await self._run_agent_loop(
            run,
            definition,
            lease,
            context_items=context_items,
            prior_tool_outcomes=prior_outcomes,
        )

    async def _enter_waiting_definition_unavailable(
        self, run: RunRecord, lease: RunLease
    ) -> RunRecord:
        """ADR 0023：原版本不可用 -> WAITING + 机器可读 reason，
        绝不静默回退到最新定义。"""
        result = await self._store.transition_run(
            run.run_id,
            expected_version=run.version,
            status=RunStatus.WAITING,
            waiting_reason=REASON_DEFINITION_UNAVAILABLE,
            lease_owner=lease.owner,
        )
        self._publish_status(run.run_id, RunStatus.WAITING)
        return result

    async def _execute_steps(
        self,
        run: RunRecord,
        definition: AgentDefinition,
        lease: RunLease,
    ) -> RunRecord:
        """按 Definition 声明推进一个 RUNNING Run 的全部步骤。

        执行顺序（ADR 0015 / PRD User Story 25）：若 Definition 声明了
        Context Provider，先执行 Context Step 并完成 checkpoint，再执行
        Model Step——应用选择的上下文不依赖模型工具选择，且外部上下文
        的 checkpoint 先于依赖它的模型调用持久化。任一 Step 失败（Run
        到达 FAILED）即返回，不再继续后续 Step。

        Ticket 08：入口即安全边界——已请求取消时不再启动任何 Step，
        直接终结为 CANCELLED（ADR 0012 / AC 8）。
        """
        cancelled = await self._maybe_cancel(run, lease)
        if cancelled is not None:
            return cancelled
        context_items: list[ContextItem] = []
        if definition.context_provider is not None:
            run, context_items = await self._run_context_step(
                run, definition, lease
            )
            if run.status is not RunStatus.RUNNING:
                return run
        return await self._run_agent_loop(
            run, definition, lease, context_items=context_items
        )

    async def _run_context_step(
        self,
        run: RunRecord,
        definition: AgentDefinition,
        lease: RunLease,
    ) -> tuple[RunRecord, list[ContextItem]]:
        """执行一个 Context Step 并返回 (最新 Run 记录, 本次 Context Items)。

        成功：记录 CONTEXT Step + Step Attempt + Checkpoint（输出为
        Context Items 的序列化载荷），Run 保持 RUNNING；调用方随后
        把 Items 作为数据交给 Model Step。
        失败：记录 FAILED Step + FAILED Step Attempt（可检查，绝不
        压平成模型可见的上下文字符串），Run 到达终态 FAILED 并释放
        租约，不继续执行 Model Step。
        """
        provider = definition.context_provider
        if provider is None:  # 防御：仅由有 provider 的路径调用
            raise RuntimeError("definition declares no context provider")
        step_id = new_id()
        cancelled = await self._maybe_cancel(run, lease)
        if cancelled is not None:
            return cancelled, []
        await self._assert_step_dispatch(run, lease)
        attempt_id = new_id()
        self._publish(
            RunUpdate(
                run_id=run.run_id,
                update_type=RunUpdateType.STEP_STARTED,
                step_id=step_id,
                attempt_id=attempt_id,
                step_type=StepType.CONTEXT,
            )
        )
        self._telemetry_started(run.run_id, step_id, attempt_id)
        self._emit_telemetry(
            TelemetryEvent(
                event_type=TelemetryEventType.STEP_STARTED,
                run_id=run.run_id,
                step_id=step_id,
                attempt_id=attempt_id,
                step_type=StepType.CONTEXT,
            )
        )
        # Provider dispatch 前持久化权威 Step / Attempt identity。若进程在
        # 调用返回、但 Context checkpoint 前中断，RunStore 仍保留本次
        # 外部读取的可检查证据；成功或失败会以同一 identity 更新状态。
        await self._store.record_step(
            StepRecord(
                step_id=step_id,
                run_id=run.run_id,
                step_type=StepType.CONTEXT,
                status=StepStatus.RUNNING,
            ),
            expected_version=run.version,
            lease_owner=lease.owner,
        )
        await self._store.record_attempt(
            StepAttempt(
                attempt_id=attempt_id,
                step_id=step_id,
                run_id=run.run_id,
                status=StepStatus.RUNNING,
            ),
            expected_version=run.version,
            lease_owner=lease.owner,
        )
        try:
            # Telemetry Sink 是应用提供的同步回调，可能耗时到租约过期。
            # 外部 provider dispatch 前必须在最后边界重新读取权威租约。
            await self._assert_step_dispatch(run, lease)
            cancelled = await self._maybe_cancel(run, lease)
            if cancelled is not None:
                return cancelled, []
            items = list(
                await provider.provide(ContextRequest(input=run.input))
            )
            # Provider 输出只有能完整序列化为受保护 Checkpoint 才算成功。
            # metadata 是外部数据；序列化失败与 provider 异常一样形成可
            # 检查的 FAILED Attempt，绝不遗留 RUNNING identity。
            payload = serialize_context_items(items)
        except (LeaseNotHeldError, StaleRunVersionError):
            raise
        except Exception as exc:  # Provider 失败：可检查的失败 Attempt
            await self._store.record_step(
                StepRecord(
                    step_id=step_id,
                    run_id=run.run_id,
                    step_type=StepType.CONTEXT,
                    status=StepStatus.FAILED,
                ),
                expected_version=run.version,
                lease_owner=lease.owner,
            )
            await self._record_failed_attempt(
                run,
                lease,
                step_id,
                classify_exception(exc),
                StepType.CONTEXT,
                attempt_id=attempt_id,
            )
            # Provider 调用已经返回失败，失败 Attempt 是权威证据；若
            # 同时收到取消，不再把 Run 终结为 FAILED 或启动 Model Step。
            cancelled = await self._maybe_cancel(run, lease)
            if cancelled is not None:
                return cancelled, []
            result = await self._store.transition_run(
                run.run_id,
                expected_version=run.version,
                status=RunStatus.FAILED,
                lease_owner=lease.owner,
            )
            await self._store.release_lease(
                run.run_id, lease.owner, expected_version=result.version
            )
            self._publish_status(run.run_id, RunStatus.FAILED)
            return result, []

        self._maybe_crash(CrashPoint.BEFORE_CONTEXT_CHECKPOINT, run.run_id)
        await self._store.record_step(
            StepRecord(
                step_id=step_id,
                run_id=run.run_id,
                step_type=StepType.CONTEXT,
                status=StepStatus.SUCCEEDED,
            ),
            expected_version=run.version,
            lease_owner=lease.owner,
        )
        attempt = StepAttempt(
            attempt_id=attempt_id,
            step_id=step_id,
            run_id=run.run_id,
            status=StepStatus.SUCCEEDED,
            output=payload,
        )
        await self._store.record_attempt(
            attempt,
            expected_version=run.version,
            lease_owner=lease.owner,
        )
        await self._store.record_checkpoint(
            StepCheckpoint(
                run_id=run.run_id,
                step_id=step_id,
                attempt_id=attempt.attempt_id,
                step_type=StepType.CONTEXT,
                output=payload,
            ),
            expected_version=run.version,
            lease_owner=lease.owner,
        )
        self._maybe_crash(CrashPoint.AFTER_CONTEXT_CHECKPOINT, run.run_id)
        self._publish(
            RunUpdate(
                run_id=run.run_id,
                update_type=RunUpdateType.STEP_COMPLETED,
                step_id=step_id,
                attempt_id=attempt.attempt_id,
                step_type=StepType.CONTEXT,
            )
        )
        self._emit_telemetry(
            TelemetryEvent(
                event_type=TelemetryEventType.STEP_COMPLETED,
                run_id=run.run_id,
                step_id=step_id,
                attempt_id=attempt.attempt_id,
                step_type=StepType.CONTEXT,
                step_status=StepStatus.SUCCEEDED,
                duration_ms=self._telemetry_duration_ms(
                    run.run_id, step_id, attempt.attempt_id
                ),
            )
        )
        return run, items

    async def _run_agent_loop(
        self,
        run: RunRecord,
        definition: AgentDefinition,
        lease: RunLease,
        context_items: Sequence[ContextItem] = (),
        prior_tool_outcomes: Sequence[ToolOutcome] = (),
        resumed_model_step_id: str | None = None,
    ) -> RunRecord:
        """模型-工具循环：推进一个 RUNNING Run 直到最终响应（Ticket 05）。

        每次模型调用都是一个独立 Model Step（记录 Step / Attempt /
        Checkpoint，checkpoint 携带完整响应，ADR 0004 / PRD US 14）；
        若响应请求了工具，则同一响应内的每个工具调用**严格顺序**执行
        为一个独立 Tool Step（记录 Step / Attempt / Checkpoint），把
        Tool Outcome 作为外部数据追加进下一次模型请求（ADR 0017 /
        0024），然后继续循环；直到模型响应不再请求工具，该响应成为
        Run 的最终输出并写入 SUCCEEDED。

        - SUCCESS 与 REJECTED 都是显式 Tool Outcome，正常 checkpoint；
        - 工具未捕获异常记录失败 Attempt（结构化分类 + 错误标识 +
          时间证据），Run 到达 FAILED（或 UNCERTAIN NON_IDEMPOTENT
          时 WAITING），绝不包装成模型可见的自然语言工具结果；
        - 模型异常同样记录失败 Attempt、Run 到达 FAILED。
        循环由模型响应决定终止（响应不再请求工具即结束）；本版本
        不引入隐藏的工具轮次上限，因此模型的持续工具请求会持续被
        顺序执行（自动重试只由冻结的 Retry Policy 驱动，见
        Ticket 06 / ADR 0025）。
        """
        tool_outcomes = tuple(prior_tool_outcomes)
        model_step_id = resumed_model_step_id
        while True:
            # 安全边界：新 Model Step 之前检查协作取消（ADR 0012）。
            cancelled = await self._maybe_cancel(run, lease)
            if cancelled is not None:
                return cancelled
            run, response = await self._run_single_model_step(
                run,
                definition,
                lease,
                context_items,
                tool_outcomes,
                step_id=model_step_id,
            )
            model_step_id = None
            if response is None:  # 模型失败 -> Run FAILED
                return run
            # 完整 Model response 已按 _run_single_model_step 的语义
            # checkpoint，才到达这个安全边界。这里必须重新消费并发
            # cancel request：已 dispatch 的调用不能被描述成中断或回滚，
            # 但也绝不能据此开始 Tool Step 或提交 SUCCEEDED。
            cancelled = await self._maybe_cancel(run, lease)
            if cancelled is not None:
                return cancelled
            if not response.tool_calls:
                # 最终响应：写入终态并释放租约。
                result = await self._store.transition_run(
                    run.run_id,
                    expected_version=run.version,
                    status=RunStatus.SUCCEEDED,
                    output=response.content,
                    lease_owner=lease.owner,
                )
                await self._store.release_lease(
                    run.run_id, lease.owner, expected_version=result.version
                )
                self._publish_status(run.run_id, RunStatus.SUCCEEDED)
                return result
            # 同一响应内的多个工具调用严格顺序执行（ADR 0004 / PRD
            # US 43）：上一个调用完成并 checkpoint 后才执行下一个。
            for call in response.tool_calls:
                # 安全边界：每个 Tool Step 开始之前检查协作取消——
                # 取消只阻止后续 Step，不打断已完成/已发出的调用。
                cancelled = await self._maybe_cancel(run, lease)
                if cancelled is not None:
                    return cancelled
                run, outcome = await self._run_tool_step(
                    run, definition, lease, call
                )
                if outcome is None:  # 工具失败 -> Run FAILED
                    return run
                tool_outcomes = (*tool_outcomes, outcome)

    async def _run_single_model_step(
        self,
        run: RunRecord,
        definition: AgentDefinition,
        lease: RunLease,
        context_items: Sequence[ContextItem],
        tool_outcomes: Sequence[ToolOutcome],
        step_id: str | None = None,
    ) -> tuple[RunRecord, ModelResponse | None]:
        """执行一次模型调用并记录 Model Step / Attempt / Checkpoint。

        返回 ``(最新 Run 记录, 模型响应)``；模型失败且不再重试时 Run
        到达 FAILED，返回 ``(run, None)``。请求携带 Definition 的
        Agent Instruction（受信）与作为数据的 Context Items / Tool
        Outcomes（ADR 0017），Tool Outcome 绝不写入或替换 instructions。

        Ticket 06 重试语义（ADR 0025）：失败分类是 Model Adapter
        结构化契约的一部分（:class:`StepFailure`，绝不解析异常消息）。
        只有冻结在 Definition Snapshot 中的显式 Retry Policy 允许
        自动重试只接受 TRANSIENT 且未超过 ``max_attempts`` 的失败，并
        创建**新的 Step Attempt**（同一 step_id，新 attempt_id）；
        PERMANENT、UNCERTAIN 或未分类失败（fail-closed）不重试；无策略
        不重试。
        每个失败 Attempt 都保留 classification / error_code / error /
        created_at 证据。

        Ticket 08 流式（ADR 0011）：Adapter 声明 ``streaming=DELTA`` 时
        通过 :meth:`_stream_model` 消费流——每个增量发布为带
        run_id / step_id / attempt_id 的 ``MODEL_DELTA`` Run Update
        （AC 2），增量**不写入 Run Store**（AC 3）；只有流结束的完整
        :class:`ModelResponse` 才 checkpoint（AC 4）。每次 attempt 发布
        ``STEP_STARTED``，失败发布 ``ATTEMPT_FAILED``，成功 checkpoint
        后发布 ``STEP_COMPLETED``。流内 delta 之间检查协作取消：已请求
        取消时协作中断流并终结为 CANCELLED（ADR 0012 / AC 8）。
        """
        if run.snapshot is None:
            raise RuntimeError(
                f"run {run.run_id} has no frozen snapshot; "
                "cannot execute a model step"
            )
        self._assert_adapter_contract_matches_snapshot(run, definition)
        # 重试决策只依据 Run 启动时冻结的 Retry Policy（ADR 0022/0023），
        # 运行中修改 Agent Definition 不能改变已有 Run 的重试行为。
        policy = run.snapshot.retry_policy
        adapter = definition.model_adapter
        purpose = ModelPurpose.PRIMARY
        binding = run.snapshot.model_bindings.for_purpose(purpose)
        model_contract = binding.contract
        streaming = (
            model_contract.capabilities.streaming is StreamingMode.DELTA
        )
        step_id = step_id if step_id is not None else new_id()
        persisted_attempts = [
            attempt
            for attempt in await self._store.get_attempts(run.run_id)
            if attempt.step_id == step_id
        ]
        attempt_count = len(persisted_attempts)
        if attempt_count:
            last_attempt = persisted_attempts[-1]
            if (
                last_attempt.status is StepStatus.FAILED
                and not self._should_retry(
                    last_attempt.classification
                    if last_attempt.classification is not None
                    else FailureClassification.PERMANENT,
                    policy,
                    attempt_count,
                )
            ):
                await self._record_failed_step(
                    run, lease, step_id, StepType.MODEL
                )
                return await self._fail_run(run, lease), None
        while True:
            attempt_count += 1
            # 安全边界：发起新的 Step Attempt 之前检查协作取消。
            cancelled = await self._maybe_cancel(run, lease)
            if cancelled is not None:
                return cancelled, None
            await self._assert_step_dispatch(run, lease)
            cancelled = await self._maybe_cancel(run, lease)
            if cancelled is not None:
                return cancelled, None
            request = ModelRequest(
                input=run.input,
                instructions=run.snapshot.instructions,
                context_items=tuple(context_items),
                tools=tuple(tool.spec() for tool in definition.tools),
                tool_outcomes=tuple(tool_outcomes),
            )
            try:
                assert_model_request_compatible(model_contract, request)
            except ModelContractViolationError as exc:
                await self._record_failed_step(
                    run, lease, step_id, StepType.MODEL
                )
                return await self._fail_run(run, lease, exc.code), None
            attempt_id = new_id()
            self._publish(
                RunUpdate(
                    run_id=run.run_id,
                    update_type=RunUpdateType.STEP_STARTED,
                    step_id=step_id,
                    attempt_id=attempt_id,
                    step_type=StepType.MODEL,
                )
            )
            self._telemetry_started(run.run_id, step_id, attempt_id)
            self._emit_telemetry(
                TelemetryEvent(
                    event_type=TelemetryEventType.STEP_STARTED,
                    run_id=run.run_id,
                    step_id=step_id,
                    attempt_id=attempt_id,
                    step_type=StepType.MODEL,
                )
            )
            terminal_error_code: str | None = None
            reserved = False
            try:
                # STEP_STARTED telemetry 是应用回调；它返回后再次校验，
                # 使 guard 紧贴真正的 Model dispatch。
                await self._assert_step_dispatch(run, lease)
                cancelled = await self._maybe_cancel(run, lease)
                if cancelled is not None:
                    return cancelled, None
                self._assert_adapter_contract_matches_snapshot(
                    run, definition
                )
                assert_model_request_compatible(model_contract, request)
                reserved = await self._reserve_model_attempt(
                    run, lease, step_id, attempt_id, purpose
                )
                if not reserved:
                    await self._record_failed_step(
                        run, lease, step_id, StepType.MODEL
                    )
                    return (
                        await self._fail_run(
                            run,
                            lease,
                            ERROR_MODEL_EXECUTION_BUDGET_EXCEEDED,
                        ),
                        None,
                    )
                if streaming:
                    response = await self._stream_model(
                        adapter,
                        request,
                        run,
                        lease,
                        step_id,
                        attempt_id,
                    )
                    if response is None:
                        # 流内安全边界已取消：Run 已转 CANCELLED。
                        return await self._get_existing_run(run.run_id), None
                else:
                    response = await adapter.generate(request)
                response = normalize_model_response(model_contract, response)
            except (LeaseNotHeldError, StaleRunVersionError):
                raise
            except Exception as exc:  # 模型失败：结构化分类 + 失败 Attempt
                if isinstance(exc, ModelContractViolationError):
                    classification = FailureClassification.PERMANENT
                    code = exc.code
                    message = str(exc)
                    terminal_error_code = code
                else:
                    classification, code, message = classify_exception(exc)
                failure = (classification, code, message)
                if reserved:
                    # Reservation 已在外部 dispatch 前持久化；将同一 identity
                    # 更新为 FAILED，恢复时才能如实保留已消耗的预算。
                    await self._record_failed_attempt(
                        run,
                        lease,
                        step_id,
                        failure,
                        StepType.MODEL,
                        attempt_id=attempt_id,
                        model_purpose=purpose,
                    )
                # 已 dispatch 的 Model 调用已经如实形成失败 Attempt；
                # 取消请求到达时不启动 retry，也不以 FAILED 覆盖取消。
                if self._cancel_requested(run.run_id):
                    await self._record_failed_step(
                        run, lease, step_id, StepType.MODEL
                    )
                    cancelled = await self._maybe_cancel(run, lease)
                    if cancelled is not None:
                        return cancelled, None
                if self._should_retry(
                    classification, policy, attempt_count
                ):
                    await self._sleep(policy.delay)
                    continue
                # 不再重试：记录 FAILED Step（最终状态）并到达终态 FAILED。
                await self._record_failed_step(
                    run, lease, step_id, StepType.MODEL
                )
                cancelled = await self._maybe_cancel(run, lease)
                if cancelled is not None:
                    return cancelled, None
                result = await self._fail_run(run, lease, terminal_error_code)
                return result, None

            # 成功：Step + Attempt + 完成的 Checkpoint 依次持久化（checkpoint
            # 先于后续工作，ADR 0006）。Checkpoint 携带**完整序列化响应**
            # （含 tool_calls），恢复时据此精确重建执行位置；流式增量
            # 从不进入 checkpoint（ADR 0011）。每一步写入都携带租约 owner，
            # 租约被接管后这些迟到写入会被 Store 拒绝。
            payload = serialize_model_response(response)
            self._maybe_crash(CrashPoint.BEFORE_MODEL_CHECKPOINT, run.run_id)
            await self._store.record_step(
                StepRecord(
                    step_id=step_id,
                    run_id=run.run_id,
                    step_type=StepType.MODEL,
                    status=StepStatus.SUCCEEDED,
                ),
                expected_version=run.version,
                lease_owner=lease.owner,
            )
            attempt = StepAttempt(
                attempt_id=attempt_id,
                step_id=step_id,
                run_id=run.run_id,
                status=StepStatus.SUCCEEDED,
                output=payload,
                model_purpose=purpose,
            )
            await self._store.record_attempt(
                attempt,
                expected_version=run.version,
                lease_owner=lease.owner,
            )
            await self._store.record_checkpoint(
                StepCheckpoint(
                    run_id=run.run_id,
                    step_id=step_id,
                    attempt_id=attempt.attempt_id,
                    step_type=StepType.MODEL,
                    output=payload,
                ),
                expected_version=run.version,
                lease_owner=lease.owner,
            )
            self._maybe_crash(CrashPoint.AFTER_MODEL_CHECKPOINT, run.run_id)
            self._publish(
                RunUpdate(
                    run_id=run.run_id,
                    update_type=RunUpdateType.STEP_COMPLETED,
                    step_id=step_id,
                    attempt_id=attempt_id,
                    step_type=StepType.MODEL,
                )
            )
            # Ticket 09：Model Step 完成事件携带 Adapter 提供的 usage
            # （仅当响应实际携带 usage 时；绝不回填或猜测）。
            self._emit_telemetry(
                TelemetryEvent(
                    event_type=TelemetryEventType.STEP_COMPLETED,
                    run_id=run.run_id,
                    step_id=step_id,
                    attempt_id=attempt_id,
                    step_type=StepType.MODEL,
                    step_status=StepStatus.SUCCEEDED,
                    duration_ms=self._telemetry_duration_ms(
                        run.run_id, step_id, attempt_id
                    ),
                    usage=response.usage,
                )
            )
            return run, response

    async def _run_tool_step(
        self,
        run: RunRecord,
        definition: AgentDefinition,
        lease: RunLease,
        call: ToolCall,
        *,
        step_id: str | None = None,
        recovery_replay: bool = False,
        explicit_resolution: bool = False,
    ) -> tuple[RunRecord, ToolOutcome | None]:
        """执行一次工具调用并记录独立 Tool Step / Attempt / Checkpoint。

        每个工具调用形成一个独立 Tool Step（ADR 0004 / PRD US 16），
        在下一个工具调用或模型调用之前完成 checkpoint（PRD US 18）。

        - 显式 ``SUCCESS`` / ``REJECTED`` Outcome 都是正常完成：记录
          SUCCEEDED Step + Attempt + Checkpoint（ADR 0024），Outcome
          作为外部数据回到模型循环；
        - 未捕获异常：记录 FAILED Step Attempt（结构化分类 + 错误标识
          + 时间证据），**绝不**把异常包装成自然语言工具结果交给模型
          （ADR 0024）。

        ``step_id`` 可选（Ticket 07）：RETRY_STEP 显式授权重试时传入
        WAITING 记录的 step_id，从而在同一 Run Step 下创建**新的 Step
        Attempt**（历史 Attempt 保留）；默认生成全新 Step。

        Ticket 06 重试语义（ADR 0025）：只有冻结的 Retry Policy 允许
        自动重试，且必须同时满足——分类为 TRANSIENT、未超过
        ``max_attempts``，以及 Tool Effect 允许（**UNCERTAIN
        NON_IDEMPOTENT 绝不自动重放**：Run 进入 WAITING，等待上层应用
        显式处置，Ticket 07）。
        每次重试都创建新的 Step Attempt（同一 step_id，新 attempt_id），
        历史 Attempt 保留。

        Ticket 08：每次 attempt 发布 ``STEP_STARTED``，失败发布
        ``ATTEMPT_FAILED``，checkpoint 后由 :meth:`_checkpoint_tool_outcome`
        发布 ``STEP_COMPLETED``；发起新 attempt 之前检查协作取消
        （ADR 0012 / AC 8）。in-flight 的工具调用**不会被取消打断**：
        取消只阻止后续 Step，已发出的调用正常完成并按需 checkpoint
        （AC 9——绝不假装撤销已发生的外部副作用）。

        返回 ``(最新 Run 记录, outcome)``；失败且不再重试时 outcome 为 None。
        """
        self._assert_adapter_contract_matches_snapshot(run, definition)
        # 重试决策只依据冻结的 Definition Snapshot（含 Tool Effect 声明
        # 与 Retry Policy），运行中修改 Agent Definition 不影响已有 Run。
        policy = run.snapshot.retry_policy if run.snapshot is not None else None
        step_id = step_id if step_id is not None else new_id()
        frozen_effect = self._frozen_tool_effect(run, call.tool_name)
        persisted_attempts = [
            attempt
            for attempt in await self._store.get_attempts(run.run_id)
            if attempt.step_id == step_id
        ]
        attempt_count = len(persisted_attempts)
        if frozen_effect is None and attempt_count:
            await self._record_failed_step(run, lease, step_id, StepType.TOOL)
            return await self._fail_run(run, lease), None
        if attempt_count and not explicit_resolution:
            if recovery_replay:
                # ``RUNNING`` means this Tool dispatch was already issued
                # before its checkpoint was interrupted. At-least-once
                # recovery may issue one more call only when the frozen
                # policy has budget for that new Attempt. The interrupted
                # Attempt counts toward the same authoritative Step limit.
                if policy is None or attempt_count >= policy.max_attempts:
                    await self._record_failed_step(
                        run, lease, step_id, StepType.TOOL
                    )
                    return await self._fail_run(run, lease), None
            else:
                last_attempt = persisted_attempts[-1]
                if (
                    last_attempt.status is StepStatus.FAILED
                    and not self._should_retry(
                        last_attempt.classification
                        if last_attempt.classification is not None
                        else FailureClassification.PERMANENT,
                        policy,
                        attempt_count,
                        frozen_effect,
                    )
                ):
                    await self._record_failed_step(
                        run, lease, step_id, StepType.TOOL
                    )
                    return await self._fail_run(run, lease), None
        tool: Tool | None = None
        while True:
            attempt_count += 1
            # 安全边界：发起新的 Step Attempt 之前检查协作取消。
            cancelled = await self._maybe_cancel(run, lease)
            if cancelled is not None:
                return cancelled, None
            await self._assert_step_dispatch(run, lease)
            attempt_id = new_id()
            self._publish(
                RunUpdate(
                    run_id=run.run_id,
                    update_type=RunUpdateType.STEP_STARTED,
                    step_id=step_id,
                    attempt_id=attempt_id,
                    step_type=StepType.TOOL,
                )
            )
            self._telemetry_started(run.run_id, step_id, attempt_id)
            self._emit_telemetry(
                TelemetryEvent(
                    event_type=TelemetryEventType.STEP_STARTED,
                    run_id=run.run_id,
                    step_id=step_id,
                    attempt_id=attempt_id,
                    step_type=StepType.TOOL,
                )
            )
            # Establish the authoritative Step and Attempt identity before
            # the external tool effect. The same IDs are updated on outcome
            # or failure; a crash leaves this IN_FLIGHT evidence queryable.
            await self._store.record_step(
                StepRecord(
                    step_id=step_id,
                    run_id=run.run_id,
                    step_type=StepType.TOOL,
                    status=StepStatus.RUNNING,
                ),
                expected_version=run.version,
                lease_owner=lease.owner,
            )
            await self._store.record_attempt(
                StepAttempt(
                    attempt_id=attempt_id,
                    step_id=step_id,
                    run_id=run.run_id,
                    status=StepStatus.RUNNING,
                ),
                expected_version=run.version,
                lease_owner=lease.owner,
            )
            try:
                tool = self._find_tool(definition, call.tool_name)
                if frozen_effect is None:
                    raise ToolFailure(
                        FailureClassification.PERMANENT,
                        ERROR_FROZEN_TOOL_DECLARATION_UNAVAILABLE,
                        "frozen snapshot has no unique declaration for "
                        f"{call.tool_name!r}",
                    )
                request = ToolRequest(
                    call_id=call.call_id,
                    tool_name=call.tool_name,
                    arguments=call.arguments,
                )
                # STEP_STARTED telemetry 是应用回调；它返回后再次校验，
                # 使 guard 紧贴真正的 Tool effect dispatch。
                await self._assert_step_dispatch(run, lease)
                cancelled = await self._maybe_cancel(run, lease)
                if cancelled is not None:
                    return cancelled, None
                self._assert_adapter_contract_matches_snapshot(
                    run, definition
                )
                outcome = await tool.invoke(
                    request
                )
                # 工具必须显式返回 ToolOutcome（ADR 0024）。任何其他返回值
                # 或序列化失败都视为意外错误：记录失败 Attempt，绝不把
                # 非结构化结果当作模型可见的工具结果。
                if not isinstance(outcome, ToolOutcome):
                    raise TypeError(
                        f"tool {call.tool_name!r} returned "
                        f"{type(outcome).__name__}, expected ToolOutcome"
                    )
                # Revalidate objects created through ``model_construct`` and
                # bind every outcome to the single call that owns this Step.
                outcome = ToolOutcome.model_validate(outcome.model_dump())
                if (
                    outcome.call_id != call.call_id
                    or outcome.tool_name != call.tool_name
                ):
                    raise ValueError(
                        "tool outcome does not match the dispatched call"
                    )
                serialize_tool_outcome(outcome)
            except (LeaseNotHeldError, StaleRunVersionError):
                raise
            except Exception as exc:  # 意外异常：失败 Attempt，不交给模型
                classification, code, message = classify_exception(exc)
                failure = (classification, code, message)
                await self._record_failed_attempt(
                    run,
                    lease,
                    step_id,
                    failure,
                    StepType.TOOL,
                    attempt_id=attempt_id,
                )
                # UNCERTAIN + NON_IDEMPOTENT：绝不自动重放（ADR 0007 /
                # PRD US 37）。无论是否配置策略都进入 WAITING，把处置
                # 权显式留给上层应用（Ticket 07）；Step 未完成，只保留
                # 失败 Attempt 证据。
                if (
                    classification is FailureClassification.UNCERTAIN
                    and frozen_effect is ToolEffect.NON_IDEMPOTENT
                ):
                    await self._record_failed_step(
                        run, lease, step_id, StepType.TOOL
                    )
                    result = await self._enter_waiting_uncertain(
                        run, lease, step_id
                    )
                    return result, None
                # 已 dispatch 的 Tool 调用已经形成失败 Attempt。已知失败
                # 可安全终结为 CANCELLED；不确定非幂等分支已在上方保留
                # WAITING，绝不由这里掩盖。
                if self._cancel_requested(run.run_id):
                    await self._record_failed_step(
                        run, lease, step_id, StepType.TOOL
                    )
                    cancelled = await self._maybe_cancel(run, lease)
                    if cancelled is not None:
                        return cancelled, None
                if self._should_retry(
                    classification, policy, attempt_count, frozen_effect
                ):
                    await self._sleep(policy.delay)
                    continue
                # 不再重试：记录 FAILED Step（最终状态）并到达终态 FAILED。
                await self._record_failed_step(
                    run, lease, step_id, StepType.TOOL
                )
                cancelled = await self._maybe_cancel(run, lease)
                if cancelled is not None:
                    return cancelled, None
                result = await self._fail_run(run, lease)
                return result, None

            await self._checkpoint_tool_outcome(
                run, lease, step_id, outcome, attempt_id=attempt_id
            )
            return run, outcome

    async def _checkpoint_tool_outcome(
        self,
        run: RunRecord,
        lease: RunLease,
        step_id: str,
        outcome: ToolOutcome,
        attempt_id: str | None = None,
    ) -> None:
        """把一次工具结果写成权威的 SUCCEEDED Step / Attempt / Checkpoint。

        成功路径（工具执行返回 outcome）与 CONFIRM_STEP（应用确认结果，
        不执行工具）共用本 helper（Ticket 07 AC 5）：二者都产生完全一致
        的持久化记录，后续恢复不区分来源。Checkpoint 先于后续工作落盘
        （ADR 0006），崩溃注入点 BEFORE/AFTER_TOOL_CHECKPOINT 在此边界。

        ``attempt_id`` 可选：工具执行路径在发起尝试时已生成 attempt_id
        并发布 ``STEP_STARTED``，必须传入同一 id 以保证事件与权威记录
        关联（ADR 0011）；CONFIRM_STEP 无预生成 attempt，传 None 时新建。
        """
        payload = serialize_tool_outcome(outcome)
        self._maybe_crash(CrashPoint.BEFORE_TOOL_CHECKPOINT, run.run_id)
        await self._store.record_step(
            StepRecord(
                step_id=step_id,
                run_id=run.run_id,
                step_type=StepType.TOOL,
                status=StepStatus.SUCCEEDED,
            ),
            expected_version=run.version,
            lease_owner=lease.owner,
        )
        attempt = StepAttempt(
            attempt_id=attempt_id if attempt_id is not None else new_id(),
            step_id=step_id,
            run_id=run.run_id,
            status=StepStatus.SUCCEEDED,
            output=payload,
        )
        await self._store.record_attempt(
            attempt,
            expected_version=run.version,
            lease_owner=lease.owner,
        )
        await self._store.record_checkpoint(
            StepCheckpoint(
                run_id=run.run_id,
                step_id=step_id,
                attempt_id=attempt.attempt_id,
                step_type=StepType.TOOL,
                output=payload,
            ),
            expected_version=run.version,
            lease_owner=lease.owner,
        )
        self._maybe_crash(CrashPoint.AFTER_TOOL_CHECKPOINT, run.run_id)
        self._publish(
            RunUpdate(
                run_id=run.run_id,
                update_type=RunUpdateType.STEP_COMPLETED,
                step_id=step_id,
                attempt_id=attempt.attempt_id,
                step_type=StepType.TOOL,
            )
        )
        self._emit_telemetry(
            TelemetryEvent(
                event_type=TelemetryEventType.STEP_COMPLETED,
                run_id=run.run_id,
                step_id=step_id,
                attempt_id=attempt.attempt_id,
                step_type=StepType.TOOL,
                step_status=StepStatus.SUCCEEDED,
                duration_ms=self._telemetry_duration_ms(
                    run.run_id, step_id, attempt.attempt_id
                ),
            )
        )

    # -- Ticket 06：结构化失败分类与有界重试辅助 ---------------------

    async def _record_failed_attempt(
        self,
        run: RunRecord,
        lease: RunLease,
        step_id: str,
        failure: tuple[FailureClassification, str, str],
        step_type: StepType,
        attempt_id: str | None = None,
        model_purpose: ModelPurpose | None = None,
    ) -> None:
        """记录一次失败 Step Attempt，保留分类 / 错误标识 / 时间证据。

        每次失败都生成新的 attempt_id，历史 Attempt 永不覆盖；同时发布
        ``ATTEMPT_FAILED`` Run Update（携带 attempt_id），供订阅者丢弃
        或替换该 attempt 的部分输出（Ticket 08 / ADR 0011）。

        ``attempt_id`` 可选：调用方（Model / Tool / Context Step）在发起
        尝试时已生成 attempt_id 并发布 ``STEP_STARTED``，失败时必须传入
        同一 id，保证"部分输出 -> 失败"事件与权威 Attempt 记录用同一
        attempt_id 关联（ADR 0011：消费者按 attempt_id 替换废弃输出）。
        """
        classification, code, message = failure
        attempt_id = attempt_id if attempt_id is not None else new_id()
        await self._store.record_attempt(
            StepAttempt(
                attempt_id=attempt_id,
                step_id=step_id,
                run_id=run.run_id,
                status=StepStatus.FAILED,
                error=message,
                classification=classification,
                error_code=code,
                model_purpose=model_purpose,
            ),
            expected_version=run.version,
            lease_owner=lease.owner,
        )
        self._maybe_crash(CrashPoint.AFTER_ATTEMPT_FAILED, run.run_id)
        self._publish(
            RunUpdate(
                run_id=run.run_id,
                update_type=RunUpdateType.ATTEMPT_FAILED,
                step_id=step_id,
                attempt_id=attempt_id,
                step_type=step_type,
            )
        )
        # Ticket 09：失败 Attempt 事件携带结构化分类 + 机器可读错误码
        # + 耗时（分类来自 Adapter 契约，绝不解析异常文本）。
        self._emit_telemetry(
            TelemetryEvent(
                event_type=TelemetryEventType.ATTEMPT_FAILED,
                run_id=run.run_id,
                step_id=step_id,
                attempt_id=attempt_id,
                step_type=step_type,
                step_status=StepStatus.FAILED,
                classification=classification,
                error_code=code,
                duration_ms=self._telemetry_duration_ms(
                    run.run_id, step_id, attempt_id
                ),
            )
        )

    async def _record_failed_step(
        self,
        run: RunRecord,
        lease: RunLease,
        step_id: str,
        step_type: StepType,
    ) -> None:
        """在 Step 到达最终失败状态时记录 FAILED StepRecord。

        Step 只在其最终状态确定时记录一次（重试期间的中间失败只保留
        Attempt 证据）；重试成功时记录 SUCCEEDED StepRecord。
        """
        await self._store.record_step(
            StepRecord(
                step_id=step_id,
                run_id=run.run_id,
                step_type=step_type,
                status=StepStatus.FAILED,
            ),
            expected_version=run.version,
            lease_owner=lease.owner,
        )

    def _should_retry(
        self,
        classification: FailureClassification,
        policy: RetryPolicy | None,
        attempt_count: int,
        effect: ToolEffect | None = None,
    ) -> bool:
        """判定一次失败是否允许自动重试（ADR 0025 / Ticket 06）。

        - 无 Retry Policy：不自动重试（fail-closed）；
        - 只有 TRANSIENT 允许进入预算判断；PERMANENT、UNCERTAIN 与
          未分类失败均不重试；
        - UNCERTAIN + NON_IDEMPOTENT Tool Effect 由调用方转为 WAITING，
          此处仍作为防御性兜底拒绝；
        - 允许的 TRANSIENT 在未超过 ``max_attempts`` 时重试。
        """
        if policy is None:
            return False
        if classification is not FailureClassification.TRANSIENT:
            return False
        if (
            classification is FailureClassification.UNCERTAIN
            and effect is ToolEffect.NON_IDEMPOTENT
        ):
            return False
        return attempt_count < policy.max_attempts

    @staticmethod
    async def _sleep(delay: timedelta) -> None:
        """重试之间的确定性固定等待（非随机 backoff；默认 0 立即重试）。"""
        total = delay.total_seconds()
        if total > 0:
            await asyncio.sleep(total)

    async def _fail_run(
        self, run: RunRecord, lease: RunLease, error_code: str | None = None
    ) -> RunRecord:
        """把 Run 转换到终态 FAILED 并释放租约（不再重试后的统一路径）。"""
        result = await self._store.transition_run(
            run.run_id,
            expected_version=run.version,
            status=RunStatus.FAILED,
            error_code=error_code,
            lease_owner=lease.owner,
        )
        await self._store.release_lease(
            run.run_id, lease.owner, expected_version=result.version
        )
        self._publish_status(run.run_id, RunStatus.FAILED)
        return result

    async def _enter_waiting_uncertain(
        self, run: RunRecord, lease: RunLease, step_id: str
    ) -> RunRecord:
        """UNCERTAIN NON_IDEMPOTENT Tool Step -> WAITING（ADR 0007 / PRD
        US 37）。reason 与目标 Step（``waiting_step_id``）机器可读，留给
        上层应用显式处置（Ticket 07）；与 DEFINITION_UNAVAILABLE 的
        WAITING 路径保持一致，不在此处释放租约——resolution 命令持有
        同一租约即可继续（同进程续约或租约过期后新 owner 接管）。
        """
        result = await self._store.transition_run(
            run.run_id,
            expected_version=run.version,
            status=RunStatus.WAITING,
            waiting_reason=REASON_UNCERTAIN_NON_IDEMPOTENT,
            waiting_step_id=step_id,
            lease_owner=lease.owner,
        )
        # 如果取消在这个 Tool 调用 in-flight 时到达，不能把不确定的
        # 外部效果伪造成 CANCELLED；WAITING 是该取消请求的安全终点，
        # 后续只能由应用提交 Resolution，不能让旧事件继续影响恢复。
        self._cancel_events.pop(run.run_id, None)
        self._publish_status(run.run_id, RunStatus.WAITING)
        return result

    async def _enter_waiting_unconfirmed(
        self,
        run: RunRecord,
        lease: RunLease,
        *,
        step_id: str | None = None,
        attempt_id: str | None = None,
    ) -> RunRecord:
        """恢复时发现未确认的 NON_IDEMPOTENT 调用 -> WAITING（Ticket 07）。

        崩溃发生在工具外部效果之后、Tool Step checkpoint 提交之前：
        checkpoint 缺失 = 该副作用是否已发生无法确认。运行时**绝不自动
        重放**非幂等工具（ADR 0007 fail-closed），而是记录一个
        UNCERTAIN 失败 Attempt（机器可读 error_code，与工具主动抛
        UNCERTAIN 时一致）并进入 WAITING，等待上层应用用 RETRY_STEP /
        CONFIRM_STEP / FAIL_RUN / CANCEL_RUN 显式处置。
        """
        step_id = step_id if step_id is not None else new_id()
        await self._record_failed_attempt(
            run,
            lease,
            step_id,
            (
                FailureClassification.UNCERTAIN,
                ERROR_EFFECT_UNCONFIRMED,
                "tool checkpoint was not committed before interruption; "
                "the external effect may have occurred",
            ),
            StepType.TOOL,
            attempt_id=attempt_id,
        )
        await self._record_failed_step(run, lease, step_id, StepType.TOOL)
        return await self._enter_waiting_uncertain(run, lease, step_id)

    async def _enter_waiting_preserving_failure(
        self,
        run: RunRecord,
        lease: RunLease,
        step_id: str,
        inflight_attempt: StepAttempt | None = None,
    ) -> RunRecord:
        """进入 WAITING，同时保留已分类的失败 Attempt。

        已持久化为 FAILED 的 Attempt 是适配器提供的事实，恢复不能把它
        覆盖成另一条 ``effect_unconfirmed`` 记录。只有仍是 RUNNING 的
        dispatch 才需要在恢复时归一为 UNCERTAIN。
        """
        attempts = [
            attempt
            for attempt in await self._store.get_attempts(run.run_id)
            if attempt.step_id == step_id
        ]
        has_failed_attempt = any(
            attempt.status is StepStatus.FAILED for attempt in attempts
        )
        if inflight_attempt is not None:
            # A prior UNCERTAIN attempt on this Step must not hide a newer
            # post-dispatch RUNNING attempt. Every interrupted dispatch has
            # its own authoritative attempt_id and must be normalized before
            # returning to WAITING.
            await self._record_failed_attempt(
                run,
                lease,
                step_id,
                (
                    FailureClassification.UNCERTAIN,
                    ERROR_EFFECT_UNCONFIRMED,
                    "tool checkpoint was not committed before interruption; "
                    "the external effect may have occurred",
                ),
                StepType.TOOL,
                attempt_id=inflight_attempt.attempt_id,
            )
            has_failed_attempt = True
        if has_failed_attempt:
            await self._record_failed_step(run, lease, step_id, StepType.TOOL)
            return await self._enter_waiting_uncertain(run, lease, step_id)
        return await self._enter_waiting_unconfirmed(
            run,
            lease,
            step_id=step_id,
            attempt_id=(
                inflight_attempt.attempt_id
                if inflight_attempt is not None
                else None
            ),
        )

    async def _fail_missing_frozen_tool_declaration(
        self,
        run: RunRecord,
        lease: RunLease,
        tool_name: str,
        *,
        step_id: str | None = None,
    ) -> RunRecord:
        """拒绝根据当前 callable 推断缺失 Snapshot 的 Tool Effect。"""
        step_id = step_id if step_id is not None else new_id()
        await self._record_failed_attempt(
            run,
            lease,
            step_id,
            (
                FailureClassification.PERMANENT,
                ERROR_FROZEN_TOOL_DECLARATION_UNAVAILABLE,
                f"frozen snapshot has no unique declaration for {tool_name!r}",
            ),
            StepType.TOOL,
        )
        await self._record_failed_step(run, lease, step_id, StepType.TOOL)
        return await self._fail_run(run, lease)

    @staticmethod
    def _frozen_tool_effect(
        run: RunRecord, tool_name: str
    ) -> ToolEffect | None:
        """返回 Run Snapshot 中唯一的 Tool Effect 声明，缺失时 fail closed。"""
        if run.snapshot is None:
            return None
        declarations = [
            declaration
            for declaration in run.snapshot.tool_declarations
            if declaration.name == tool_name
        ]
        if len(declarations) != 1:
            return None
        return declarations[0].effect

    @staticmethod
    def _find_tool(definition: AgentDefinition, tool_name: str) -> Tool:
        """按名称在 Definition 中解析工具；不存在抛 RuntimeError。"""
        for tool in definition.tools:
            if tool.name == tool_name:
                return tool
        raise RuntimeError(
            f"definition {definition.definition_id}@{definition.version} "
            f"has no tool named {tool_name!r}"
        )

    def _maybe_crash(self, point: CrashPoint, run_id: str) -> None:
        if self._crash_hook is not None:
            self._crash_hook(point, run_id)
