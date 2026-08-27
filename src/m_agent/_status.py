"""Run 生命周期状态机。

状态词汇是公开契约的一部分（ADR 0002 / CONTEXT.md）：
CREATED、RUNNING、WAITING、SUCCEEDED、REJECTED、FAILED、CANCELLED。
其中 SUCCEEDED、REJECTED、FAILED、CANCELLED 是终态。

转换校验集中在这里，Runner 与 RunStore 共用同一张合法转换表，
保证非法命令在任何入口都会被显式拒绝。
"""

from __future__ import annotations

import enum

from ._errors import IllegalRunTransitionError


class RunStatus(str, enum.Enum):
    """Agent Run 在生命周期中的当前状态。"""

    CREATED = "CREATED"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    SUCCEEDED = "SUCCEEDED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_STATUSES

    def __str__(self) -> str:  # pragma: no cover - 便捷展示
        return self.value


#: 终态集合：RUN 到达后不再接受任何推进命令。
TERMINAL_STATUSES: frozenset[RunStatus] = frozenset(
    {
        RunStatus.SUCCEEDED,
        RunStatus.REJECTED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }
)

#: 本版本 明确的合法转换表。WAITING 的转出由 resolution
#: （RETRY_STEP/CONFIRM_STEP -> RUNNING；FAIL_RUN -> FAILED；
#: CANCEL_RUN -> CANCELLED）驱动；增加协作式取消入口：
#: CREATED -> CANCELLED（开始前取消）与 RUNNING -> CANCELLED（运行中
#: 在安全边界取消，ADR 0012）。终态一律拒绝后续转换。
_ALLOWED_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.CREATED: frozenset(
        {
            RunStatus.RUNNING,
            RunStatus.WAITING,
            RunStatus.REJECTED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.SUCCEEDED,
            RunStatus.REJECTED,
            RunStatus.FAILED,
            RunStatus.WAITING,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.WAITING: frozenset(
        {RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.SUCCEEDED: frozenset(),
    RunStatus.REJECTED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}


def is_terminal(status: RunStatus | str) -> bool:
    """判断一个状态是否为终态（接受枚举或字符串）。"""
    return RunStatus(status) in TERMINAL_STATUSES


def validate_transition(current: RunStatus, target: RunStatus) -> None:
    """集中校验状态转换；非法转换抛出 IllegalRunTransitionError。"""
    if target not in _ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise IllegalRunTransitionError(
            f"illegal run transition: {current.value} -> {target.value}"
        )
