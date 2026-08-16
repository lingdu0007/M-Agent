"""Run 记录模型：RunRecord 与 RunInspection。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from ._definition import DefinitionSnapshot
from ._status import RunStatus
from ._steps import StepAttempt, StepCheckpoint, StepRecord, utc_now


class RunRecord(BaseModel):
    """Run Store 中权威的 Agent Run 记录。

    `version` 是权威 Run progress 的乐观并发控制版本号：每次状态转换
    +1；Lease、Step、Attempt 与 Checkpoint mutation 都必须携带当前
    expected version，基于过期版本的更新会被 RunStore 拒绝。
    """

    run_id: str
    definition_id: str
    definition_version: str
    input: str
    status: RunStatus = RunStatus.CREATED
    snapshot: DefinitionSnapshot | None = None
    output: str | None = None
    waiting_reason: str | None = None
    #: WAITING 时目标 Step 的 step_id（Ticket 07 / ADR 0008）。
    #: UNCERTAIN NON_IDEMPOTENT WAITING 指向等待处置的 Tool Step；
    #: DEFINITION_UNAVAILABLE WAITING 无目标 Step，为 None。
    waiting_step_id: str | None = None
    #: 终态失败的稳定 machine-readable code，不携带 provider 文本。
    error_code: str | None = None
    version: int = 1
    #: 当前租约 owner（ADR 0013）；无有效租约时为 None。
    lease_owner: str | None = None
    #: 当前租约到期时间；无有效租约时为 None。
    lease_expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class RunInspection(BaseModel):
    """通过公开 Runner 查询路径得到的 Run 可观测快照。"""

    run: RunRecord
    steps: list[StepRecord] = Field(default_factory=list)
    attempts: list[StepAttempt] = Field(default_factory=list)
    checkpoints: list[StepCheckpoint] = Field(default_factory=list)
