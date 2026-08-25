"""Durable Run 公共错误类型。

所有失败都以显式异常类型暴露给 Runtime Integrator，
而不是静默降级或返回部分结果。
"""

from __future__ import annotations


class MAgentError(Exception):
    """m_agent 运行时错误的公共基类。"""


class ModelCapabilityError(MAgentError):
    """Definition 要求的 Model Capabilities 未被所选 Model Adapter 声明。"""

    code = "MODEL_CAPABILITY_UNSUPPORTED"


class ModelContractViolationError(MAgentError):
    """Adapter response failed the frozen Model Contract at normalization."""

    code = "MODEL_CONTRACT_VIOLATION"


class DefinitionConflictError(MAgentError):
    """同一 definition_id + version 已被注册，不可变 Definition 禁止覆盖。"""


class DefinitionNotFoundError(MAgentError):
    """按精确 definition_id + version 无法解析已注册的 Definition。"""


class RunNotFoundError(MAgentError):
    """按 run_id 找不到 Run 记录。"""


class DuplicateRunError(MAgentError):
    """run_id 已存在，禁止重复创建。"""


class IllegalRunTransitionError(MAgentError):
    """请求的 Run 生命周期转换在当前状态下不合法。"""


class StaleRunVersionError(MAgentError):
    """基于过期版本号的更新被拒绝；权威 Run 记录未被改动。"""


class LeaseNotHeldError(MAgentError):
    """Runner 不持有该 Run 的有效租约，推进或提交被拒绝。

    租约可能已被其他 owner 持有，或已过期；权威 Run 记录未被改动。
    获取失败（他人持有 active lease）、提交失败（owner 不匹配或
    租约过期）与释放失败（owner 不匹配）都使用本类型。
    """


class ContextBudgetExceededError(MAgentError):
    """完整 Model Request 超过冻结的 Context Budget 硬上限。

    超限发生在 Model Step dispatch 之前，产生零 model dispatch
    （ADR 0040）。Runner 不隐式裁剪、压缩或等待，而是以
    ``CONTEXT_BUDGET_EXCEEDED`` 确定性失败。
    """

    code = "CONTEXT_BUDGET_EXCEEDED"


class ResolutionNotAllowedError(MAgentError):
    """提交的 resolution action 对当前 WAITING Run 不合法。

    例如：对 DEFINITION_UNAVAILABLE 的 WAITING Run 提交 RETRY_STEP /
    CONFIRM_STEP（没有可处置的 Tool Step）、CONFIRM_STEP 缺少应用
    确认结果，或 Run 并非 WAITING。命令被明确拒绝，权威记录未被改动。
    """
