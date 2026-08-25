"""Run Resolution 契约（ADR 0008）。

CONTEXT.md：Run Resolution 是上层应用针对 ``WAITING`` Agent Run 提交的
显式控制决定，只能要求重试或确认当前 Tool Step，或将 Agent Run 终结为
失败或取消。**模型永远没有 resolution 权限**——本模块的类型只出现在
:class:`Runner.resolve_run` 的公开控制入口，Model Adapter 契约
（ModelRequest / ModelResponse）中不存在任何 resolution 通道。

四种 resolution（ADR 0008）：

- ``RETRY_STEP``：应用依据外部证据判断重复执行是安全的，显式授权
  Runner 重新执行等待中的 Tool Step（创建**新的 Step Attempt**，同一
  step_id 下保留全部历史 Attempt）；
- ``CONFIRM_STEP(result)``：应用确认外部副作用已经发生，提供一个
  确认结果；Runner **在不执行工具**的情况下把该结果作为 Tool Outcome
  写入 Attempt 与 Checkpoint，并继续推进 Run；
- ``FAIL_RUN(reason)``：恢复不安全或不可能时，把 Run 终结为 FAILED，
  不再调用工具；
- ``CANCEL_RUN(reason)``：应用主动放弃该 Run，终结为 CANCELLED，
  不误报失败，也不再调用工具。
"""

from __future__ import annotations

import enum

from pydantic import BaseModel

from ._errors import ResolutionNotAllowedError
from ._run import RunRecord


class ResolutionAction(str, enum.Enum):
    """上层应用对 WAITING Agent Run 可提交的处置动作（机器可读）。"""

    RETRY_STEP = "RETRY_STEP"
    CONFIRM_STEP = "CONFIRM_STEP"
    FAIL_RUN = "FAIL_RUN"
    CANCEL_RUN = "CANCEL_RUN"
    CONTINUE_RUN = "CONTINUE_RUN"


class RunResolution(BaseModel, frozen=True):
    """一次显式的应用 resolution 命令（ADR 0008）。

    - ``CONFIRM_STEP`` 必须携带 ``result``：应用提供的确认结果，作为
      工具结果写入 Checkpoint（绝不重新执行工具）；
    - ``FAIL_RUN`` / ``CANCEL_RUN`` 可携带 ``reason`` 作为命令的一部分
      （本版本不新增持久化字段，reason 由上层应用自行审计记录）。
    """

    action: ResolutionAction
    #: CONFIRM_STEP 时应用提供的确认结果（机器可读，作为 Tool Outcome
    #: 的 result 交给后续模型循环）。
    result: str | None = None
    #: FAIL_RUN / CANCEL_RUN 的可选命令说明。
    reason: str | None = None
    #: 应用从 WAITING 记录读取的目标 Step；提供时必须与当前权威记录
    #: 一致。DEFINITION_UNAVAILABLE 没有 Tool Step，因此其合法值为 None。
    waiting_step_id: str | None = None

    @classmethod
    def retry_step(
        cls, *, waiting_step_id: str | None = None
    ) -> "RunResolution":
        """构造 RETRY_STEP 命令。"""
        return cls(
            action=ResolutionAction.RETRY_STEP,
            waiting_step_id=waiting_step_id,
        )

    @classmethod
    def confirm_step(
        cls, result: str, *, waiting_step_id: str | None = None
    ) -> "RunResolution":
        """构造 CONFIRM_STEP 命令，携带应用确认结果。"""
        return cls(
            action=ResolutionAction.CONFIRM_STEP,
            result=result,
            waiting_step_id=waiting_step_id,
        )

    @classmethod
    def fail_run(
        cls, reason: str | None = None, *, waiting_step_id: str | None = None
    ) -> "RunResolution":
        """构造 FAIL_RUN 命令。"""
        return cls(
            action=ResolutionAction.FAIL_RUN,
            reason=reason,
            waiting_step_id=waiting_step_id,
        )

    @classmethod
    def cancel_run(
        cls, reason: str | None = None, *, waiting_step_id: str | None = None
    ) -> "RunResolution":
        """构造 CANCEL_RUN 命令。"""
        return cls(
            action=ResolutionAction.CANCEL_RUN,
            reason=reason,
            waiting_step_id=waiting_step_id,
        )


#: UNCERTAIN NON_IDEMPOTENT WAITING 状态下全部合法的 resolution actions。
#: 保持元组公开且不可变，供上层应用与测试按机器可读的方式使用。
ALLOWED_FOR_UNCERTAIN_NON_IDEMPOTENT: tuple[ResolutionAction, ...] = (
    ResolutionAction.RETRY_STEP,
    ResolutionAction.CONFIRM_STEP,
    ResolutionAction.FAIL_RUN,
    ResolutionAction.CANCEL_RUN,
)

#: DEFINITION_UNAVAILABLE WAITING 状态下合法的 resolution actions。
#: 该状态下没有可重试/确认的 Tool Step，只允许终结 Run。
ALLOWED_FOR_DEFINITION_UNAVAILABLE: tuple[ResolutionAction, ...] = (
    ResolutionAction.FAIL_RUN,
    ResolutionAction.CANCEL_RUN,
)

ALLOWED_FOR_POLICY_RESOLUTION: tuple[ResolutionAction, ...] = (
    ResolutionAction.CONTINUE_RUN,
    ResolutionAction.FAIL_RUN,
    ResolutionAction.CANCEL_RUN,
)


def allowed_resolutions(run: RunRecord) -> tuple[ResolutionAction, ...]:
    """返回当前 WAITING Run 机器可读的合法 resolution actions。

    由 ``waiting_reason`` 决定：UNCERTAIN NON_IDEMPOTENT 允许全部四种
    （重试 / 确认 / 失败 / 取消），DEFINITION_UNAVAILABLE 只允许终结。
    Runner 的 :meth:`resolve_run` 与上层应用共用本函数，保证"只暴露该
    状态有效的 resolution actions"。
    """
    from ._runner import (  # 延迟导入避免循环依赖
        REASON_DEFINITION_UNAVAILABLE,
        REASON_POLICY_RESOLUTION_REQUIRED,
        REASON_UNCERTAIN_NON_IDEMPOTENT,
    )

    if run.waiting_reason == REASON_UNCERTAIN_NON_IDEMPOTENT:
        return ALLOWED_FOR_UNCERTAIN_NON_IDEMPOTENT
    if run.waiting_reason == REASON_DEFINITION_UNAVAILABLE:
        return ALLOWED_FOR_DEFINITION_UNAVAILABLE
    if run.waiting_reason and run.waiting_reason.startswith(
        REASON_POLICY_RESOLUTION_REQUIRED + ":"
    ):
        return ALLOWED_FOR_POLICY_RESOLUTION
    return ()


def require_allowed(run: RunRecord, action: ResolutionAction) -> None:
    """校验 action 对该 WAITING Run 合法；不合法抛 ResolutionNotAllowedError。"""
    if action not in allowed_resolutions(run):
        raise ResolutionNotAllowedError(
            f"resolution {action.value} is not allowed for run "
            f"{run.run_id} in WAITING reason {run.waiting_reason!r}"
        )
