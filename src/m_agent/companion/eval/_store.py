"""InMemory EvalStore：execution 与 observation 的不可变身份（AC 8）。

EvalStore 只做 append-only：同 id 同内容重放幂等，同 id 异内容
确定性冲突（已保存事实绝不改写）。公开 view（observation_view /
execution_view）足以支撑验收，同时不暴露未授权 payload——run
input/output/history 不进入 view。
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from ..._steps import utc_now
from ._errors import EvalRecordConflictError
from ._observation import EvalObservation

__all__ = [
    "EvalExecutionRecord",
    "EvalExecutionView",
    "EvalObservationView",
    "EvalStore",
    "InMemoryEvalStore",
]


class EvalExecutionRecord(BaseModel):
    """一次 Suite 展开/执行编排的持久化事实（mode + item 集合）。

    Ticket 17 只保存不可变身份；调度、恢复与进度属 Ticket 18。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    execution_id: str = Field(min_length=1)
    suite_id: str = Field(min_length=1)
    suite_version: str = Field(min_length=1)
    suite_digest: str = Field(min_length=1)
    mode: str = Field(min_length=1)
    item_ids: tuple[str, ...] = Field(min_length=1)
    created_at: datetime = Field(default_factory=utc_now)


class EvalObservationView(BaseModel):
    """Observation 的最小公开 view：无任何 payload 字段。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observation_id: str
    mode: str
    subject_run_id: str
    definition_id: str
    definition_version: str
    variant_id: str | None
    completeness: str
    reason_code: str
    execution_id: str | None
    collected_at: datetime


class EvalExecutionView(BaseModel):
    """Execution 的最小公开 view（关联的 observation 身份有序列出）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    execution_id: str
    suite_id: str
    suite_version: str
    suite_digest: str
    mode: str
    item_ids: tuple[str, ...]
    created_at: datetime
    observation_ids: tuple[str, ...] = ()


@runtime_checkable
class EvalStore(Protocol):
    """EvalStore 行为契约：append-only 不可变身份 + 最小公开 view。"""

    async def record_execution(
        self, execution: EvalExecutionRecord
    ) -> EvalExecutionRecord: ...

    async def get_execution(
        self, execution_id: str
    ) -> EvalExecutionRecord | None: ...

    async def record_observation(
        self, observation: EvalObservation
    ) -> EvalObservation: ...

    async def get_observation(
        self, observation_id: str
    ) -> EvalObservation | None: ...

    async def observation_view(
        self, observation_id: str
    ) -> EvalObservationView | None: ...

    async def execution_view(
        self, execution_id: str
    ) -> EvalExecutionView | None: ...


class InMemoryEvalStore:
    """进程内 append-only EvalStore（离线评估与测试默认实现）。

    SQLite 持久化实现属 Ticket 18；两者须共享本行为契约。
    """

    def __init__(self) -> None:
        self._executions: dict[str, EvalExecutionRecord] = {}
        self._observations: dict[str, EvalObservation] = {}
        self._execution_observations: dict[str, list[str]] = {}

    async def record_execution(
        self, execution: EvalExecutionRecord
    ) -> EvalExecutionRecord:
        existing = self._executions.get(execution.execution_id)
        if existing is None:
            self._executions[execution.execution_id] = execution
            self._execution_observations.setdefault(
                execution.execution_id, []
            )
            return execution
        if existing != execution:
            raise EvalRecordConflictError(
                f"execution {execution.execution_id!r} already stored "
                "with different content; eval records are immutable"
            )
        return existing

    async def get_execution(
        self, execution_id: str
    ) -> EvalExecutionRecord | None:
        return self._executions.get(execution_id)

    async def record_observation(
        self, observation: EvalObservation
    ) -> EvalObservation:
        existing = self._observations.get(observation.observation_id)
        if existing is None:
            self._observations[observation.observation_id] = observation
            if observation.execution_id is not None:
                self._execution_observations.setdefault(
                    observation.execution_id, []
                ).append(observation.observation_id)
            return observation
        if existing != observation:
            raise EvalRecordConflictError(
                f"observation {observation.observation_id!r} already "
                "stored with different content; eval records are immutable"
            )
        return existing

    async def get_observation(
        self, observation_id: str
    ) -> EvalObservation | None:
        return self._observations.get(observation_id)

    async def observation_view(
        self, observation_id: str
    ) -> EvalObservationView | None:
        observation = self._observations.get(observation_id)
        if observation is None:
            return None
        return EvalObservationView(
            observation_id=observation.observation_id,
            mode=observation.mode.value,
            subject_run_id=observation.subject_run_id,
            definition_id=observation.definition_id,
            definition_version=observation.definition_version,
            variant_id=observation.variant_id,
            completeness=observation.completeness.value,
            reason_code=observation.reason_code,
            execution_id=observation.execution_id,
            collected_at=observation.collected_at,
        )

    async def execution_view(
        self, execution_id: str
    ) -> EvalExecutionView | None:
        execution = self._executions.get(execution_id)
        if execution is None:
            return None
        return EvalExecutionView(
            execution_id=execution.execution_id,
            suite_id=execution.suite_id,
            suite_version=execution.suite_version,
            suite_digest=execution.suite_digest,
            mode=execution.mode,
            item_ids=execution.item_ids,
            created_at=execution.created_at,
            observation_ids=tuple(
                self._execution_observations.get(execution_id, ())
            ),
        )
