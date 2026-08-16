"""Run Step、Step Attempt 与 Checkpoint 记录模型。

ADR 0004：一次模型调用记录为一个 Model Step；一次 Context Provider
调用记录为一个 Context Step（ADR 0015，Ticket 04 引入）。
CONTEXT.md：Step Attempt 是一次具体执行尝试；Checkpoint 是 Run Store
中已经确认持久化、可供中断后继续执行的 Run Step 边界。
Tool Step 由后续 Ticket 引入。
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from ._model import ModelPurpose, ModelUsage

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StepType(str, enum.Enum):
    """Run Step 类型。MODEL 与 CONTEXT 由前序 Ticket 引入；TOOL 由
    Ticket 05 引入（每个独立工具调用一个 Tool Step，ADR 0004）。"""

    MODEL = "MODEL"
    CONTEXT = "CONTEXT"
    TOOL = "TOOL"


class StepStatus(str, enum.Enum):
    """Step / Step Attempt 的权威生命周期状态。"""

    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class FailureClassification(str, enum.Enum):
    """Step Failure 的标准分类（ADR 0025 / CONTEXT.md）。

    - ``TRANSIENT``：瞬时错误，重试可能成功；
    - ``PERMANENT``：确定性错误，重试不会改变结果；
    - ``UNCERTAIN``：无法确认尝试是否产生了外部效果。

    分类是 Model / Tool Adapter 契约的结构化部分，Runner 绝不通过
    解析异常消息推断分类（Ticket 06 AC 1）。
    """

    TRANSIENT = "TRANSIENT"
    PERMANENT = "PERMANENT"
    UNCERTAIN = "UNCERTAIN"


class StepRecord(BaseModel):
    """一个 Run Step 的记录（可独立记录结果的最小执行边界）。"""

    step_id: str
    run_id: str
    step_type: StepType
    status: StepStatus
    #: Stable terminal error identity for a failed Step when no Attempt exists
    #: (for example, a pre-dispatch Model budget rejection).
    error_code: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class StepAttempt(BaseModel):
    """Step 的一次具体执行尝试。

    失败尝试保留结构化分类、机器可读错误标识与时间证据
    （Ticket 06 AC 2）：``classification`` 来自 Adapter 的显式契约，
    ``error_code`` 是稳定错误标识，``error`` 是经 PayloadCodec 保护的
    人类可读消息，
    ``created_at`` 记录尝试发生的时间。每次重试产生不同的
    ``attempt_id``，历史 Attempt 永不覆盖。
    """

    attempt_id: str
    step_id: str
    run_id: str
    status: StepStatus
    output: str | None = None
    error: str | None = None
    #: 失败分类（ADR 0025）；成功 Attempt 为 None。
    classification: FailureClassification | None = None
    #: 机器可读错误标识（如 ``"rate_limited"``）；失败 Attempt 必填。
    error_code: str | None = None
    #: Model Attempt 使用的冻结 binding purpose；其他 Step 为 None。
    model_purpose: ModelPurpose | None = None
    #: Model Step 成功后的权威用量 metadata；其他 Step 为 None。
    usage: ModelUsage | None = None
    created_at: datetime = Field(default_factory=utc_now)


class StepCheckpoint(BaseModel):
    """已确认持久化的 Step 边界（中断后可据此继续执行）。

    ``step_type`` 标识 checkpoint 属于哪种 Step；恢复时 Runner 据此
    区分已确认的 Context Step 与 Model Step，只复用对应类型的
    checkpoint。
    """

    run_id: str
    step_id: str
    attempt_id: str
    step_type: StepType = StepType.MODEL
    output: str
    created_at: datetime = Field(default_factory=utc_now)
