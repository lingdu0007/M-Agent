"""结构化 Step Failure 契约（Ticket 06 / ADR 0025）。

Model 与 Tool Adapter 把失败归一化为 :class:`FailureClassification`
（``TRANSIENT`` / ``PERMANENT`` / ``UNCERTAIN``），并作为**结构化
异常**的一部分显式声明——Runner 绝不通过解析异常消息推断分类。

- :class:`ModelFailure`：Model Adapter 在 :meth:`generate` 中抛出的
  结构化失败；
- :class:`ToolFailure`：Tool 在 :meth:`invoke` 中抛出的结构化失败。

Adapter 未显式分类的裸异常由 :func:`classify_exception` 按
fail-closed 原则归一为 ``PERMANENT``（不自动重试）。运行时仅保留
异常类型构成的安全诊断，绝不把 ``str(exc)`` 的原始内容交给持久化层，
避免 provider/tool 把凭证或请求内容误带入异常时变成 Run Payload
（Ticket 02 / Ticket 06 AC 1）。
"""

from __future__ import annotations

from ._steps import FailureClassification

#: 未显式分类的裸异常的默认机器可读错误标识。
DEFAULT_FAILURE_CODE = "UNCLASSIFIED_FAILURE"
UNSAFE_ERROR_CODE = "unsafe_error_code"
REDACTED_FAILURE_MESSAGE = "adapter failure diagnostic redacted"

# StepFailure.code originates with an Adapter, which is an extension boundary.
# Only these stable, non-content-bearing identities may be written to
# queryable Attempt metadata or telemetry. Custom adapters must map their own
# provider values to one of these codes (or accept the opaque fallback).
SAFE_ERROR_CODES = frozenset(
    {
        DEFAULT_FAILURE_CODE,
        "FROZEN_TOOL_DECLARATION_UNAVAILABLE",
        "COMPRESSION_CONTRACT_VIOLATION",
        "MODEL_CAPABILITY_UNSUPPORTED",
        "MODEL_CONTRACT_VIOLATION",
        "MODEL_DISPATCH_CANCELLED",
        "POLICY_ERROR",
        "POLICY_OUTCOME_PENDING",
        "effect_unconfirmed",
        "invalid_request",
        "lookup_broken",
        "lookup_rejected",
        "model_rejected",
        "model_checkpoint_unconfirmed",
        "provider_credentials_missing",
        "provider_request_failed",
        "provider_response_invalid",
        "provider_test_error",
        "provider_transport_error",
        "provider_unavailable",
        "rate_limited",
        "upstream_down",
        "upstream_unavailable",
    }
)


def sanitize_error_code(code: str) -> str:
    """Return a safe error identity for metadata and telemetry.

    ``StepFailure.code`` is adapter-controlled input, not a trusted logging
    field. In particular it must not carry provider text, credentials, or
    request payload. Unknown values deliberately collapse to one stable,
    non-sensitive identity while their failure classification remains intact.
    """
    return code if code in SAFE_ERROR_CODES else UNSAFE_ERROR_CODE


def redact_failure_message(message: str) -> str:
    """Replace Adapter-provided diagnostic text with a safe fixed summary."""
    del message
    return REDACTED_FAILURE_MESSAGE


class StepFailure(Exception):
    """携带结构化分类的 Step 失败基类。

    :param classification: ``TRANSIENT`` / ``PERMANENT`` / ``UNCERTAIN``
        （Adapter 契约的一部分，Runner 直接读取，不解析消息）。
    :param code: 机器可读、稳定的错误标识（如 ``"rate_limited"``）。
    :param message: Adapter 提供的失败说明；运行时将其替换为固定安全
        摘要，绝不写入 Attempt metadata、payload 或 telemetry。
    """

    def __init__(
        self,
        classification: FailureClassification | str,
        code: str,
        message: str,
    ) -> None:
        self.classification = FailureClassification(classification)
        self.code = code
        self.message = message
        super().__init__(message)


class ModelFailure(StepFailure):
    """Model Adapter 显式分类的模型调用失败。"""


class ToolFailure(StepFailure):
    """Tool 显式分类的工具调用失败。"""


def classify_exception(
    exc: BaseException,
) -> tuple[FailureClassification, str, str]:
    """把适配器抛出的异常归一为 (classification, error_code, message)。

    - :class:`StepFailure`：使用其结构化分类与 allowlist 后的错误标识；
    - 其他裸异常：fail-closed 归一为 ``PERMANENT`` +
      ``UNCLASSIFIED_FAILURE``（不自动重试，绝不解析异常消息推断），
      诊断只含异常类型，不含 ``str(exc)``。
    """
    if isinstance(exc, StepFailure):
        return (
            exc.classification,
            sanitize_error_code(exc.code),
            redact_failure_message(exc.message),
        )
    return (
        FailureClassification.PERMANENT,
        DEFAULT_FAILURE_CODE,
        f"unclassified adapter exception: {type(exc).__name__}",
    )
