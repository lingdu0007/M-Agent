"""Telemetry Sink 契约与官方本地 JSONL 实现（ADR 0035）。

CONTEXT.md：Telemetry Sink 是接收带 Run、Step 和 Attempt 关联标识的
结构化 Trace 的轻量扩展边界，默认不接收 Run Payload。

本模块定义唯一公开的 Telemetry 数据契约与官方本地实现：

- :class:`TelemetryEvent` 是稳定的、逐行可解析的事件记录，携带
  ``run_id`` / ``step_id`` / ``attempt_id``、事件类型、时间 / 耗时、
  生命周期状态、标准错误分类与可用 usage（ADR 0035）；
- :class:`TelemetrySink` 是轻量接收边界：同步 ``emit(event)``，不
  接收 Run Payload（模型内容、Context Item、Tool Outcome、resolution
  载荷、凭证一律不进事件）；
- :class:`JsonlTelemetrySink` 是官方本地 sink：把每个事件写成一行
  JSON（JSONL），无需 observability 服务器或 Dashboard 即可检查。

Telemetry 只用于观测（ADR 0006 / 0010）：RunStore 仍是唯一权威。
Sink 自身失败由 Runner 隔离（捕获并继续推进，绝不覆盖或伪造
RunStore 状态）。官方 OpenTelemetry projection lives in the Adapter layer,
not this Core module; no Dashboard、集中日志服务、payload opt-in UI 或
持久事件总线。
"""

from __future__ import annotations

import enum
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field, field_validator

from ._failure import sanitize_error_code
from ._model import ModelPurpose, ModelUsage
from ._status import RunStatus
from ._steps import (
    FailureClassification,
    StepStatus,
    StepType,
    utc_now,
)

try:  # Linux/macOS HOST evidence requires process-safe append serialization.
    import fcntl
except ImportError:  # pragma: no cover - Windows is outside this roadmap.
    fcntl = None  # type: ignore[assignment]


class TelemetryEventType(str, enum.Enum):
    """Telemetry 事件的类型（机器可读，稳定标识符）。

    - ``RUN_STATUS_CHANGED``：Run 生命周期状态转换（含 CREATED /
      RUNNING / WAITING / 终态），携带 ``run_status``；
    - ``STEP_STARTED``：一个新的 Step Attempt 开始，携带
      ``step_id`` / ``attempt_id`` / ``step_type``；
    - ``STEP_COMPLETED``：一个 Step 成功并已 checkpoint，携带
      ``step_id`` / ``attempt_id`` / ``step_type`` / ``step_status`` /
      ``duration_ms``；Model Step 还携带可用 ``usage``；
    - ``ATTEMPT_FAILED``：一个 Step Attempt 失败，携带 ``step_id`` /
      ``attempt_id`` / ``step_type`` / ``step_status`` /
      ``classification`` / ``error_code`` / ``duration_ms``。
    """

    RUN_STATUS_CHANGED = "RUN_STATUS_CHANGED"
    STEP_STARTED = "STEP_STARTED"
    STEP_COMPLETED = "STEP_COMPLETED"
    ATTEMPT_FAILED = "ATTEMPT_FAILED"


class TelemetryEvent(BaseModel, frozen=True):
    """一条结构化、非权威的 Telemetry 记录（ADR 0035）。

    所有字段均为纯数据，``model_dump_json()`` 产出一行可解析 JSON。
    事件**默认不含 Run Payload**：Run 输入、模型内容、Context Item
    内容、Tool Outcome 载荷、resolution 载荷与凭证绝不进入事件。
    """

    event_type: TelemetryEventType
    run_id: str
    #: 关联的 Run Step；非 Step 级事件（RUN_STATUS_CHANGED）为 None。
    step_id: str | None = None
    #: 关联的 Step Attempt；``STEP_STARTED`` / ``STEP_COMPLETED`` /
    #: ``ATTEMPT_FAILED`` 必填。
    attempt_id: str | None = None
    step_type: StepType | None = None
    #: Model Step 的冻结用途；仅 Model Step 事件携带（PRIMARY /
    #: CONTEXT_COMPRESSION / OUTPUT_REPAIR），用于与权威 Attempt 对账。
    model_purpose: ModelPurpose | None = None
    #: ``RUN_STATUS_CHANGED`` 时的新 Run Status。
    run_status: RunStatus | None = None
    #: ``STEP_COMPLETED``（SUCCEEDED）与 ``ATTEMPT_FAILED``（FAILED）时
    #: 的 Step 完成状态。
    step_status: StepStatus | None = None
    #: ``ATTEMPT_FAILED`` 时的失败分类（TRANSIENT / PERMANENT /
    #: UNCERTAIN；来自 Adapter 结构化契约，绝不解析异常文本）。
    classification: FailureClassification | None = None
    #: ``ATTEMPT_FAILED`` 时的机器可读错误标识（如 ``"rate_limited"``）。
    error_code: str | None = None
    #: Step Attempt 从 ``STEP_STARTED`` 到完成的耗时（毫秒）；只有
    #: 起点已记录（本进程内发起）时才有值。
    duration_ms: float | None = None
    #: ``STEP_COMPLETED``（Model Step）时 Adapter 提供的用量；仅当
    #: Adapter 声明 usage_reporting 且响应携带 usage 时有值。
    usage: ModelUsage | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("error_code")
    @classmethod
    def _sanitize_error_code(cls, value: str | None) -> str | None:
        """Defence in depth for direct TelemetryEvent construction."""
        return sanitize_error_code(value) if value is not None else None


@runtime_checkable
class TelemetrySink(Protocol):
    """Telemetry 接收边界。

    - ``emit(event)`` 是轻量同步接口，只接收 :class:`TelemetryEvent`，
      不接收 Run Payload；
    - 实现方负责自己的传输 / 存储 / 丢弃策略；核心不强依赖任何
      observability SDK（ADR 0035）；
    - Sink 失败由 Runner 隔离（捕获并继续推进 Run），实现方抛出的
      任何异常都不得覆盖或伪造 RunStore 状态。
    """

    def emit(self, event: TelemetryEvent) -> None: ...


class JsonlTelemetrySink:
    """官方本地 JSONL sink：把每个事件写为本地文件的一行 JSON。

    - 惰性打开：首次 ``emit`` 时以 append 模式创建 / 打开文件，每次
      ``emit`` 后立即 flush，本地检查无需等待进程结束；
    - 线程安全：多个 Runner / 线程可共享同一实例写同一文件（本地
      收集多个 Run 的 telemetry）；
    - 父目录不存在等写失败会抛异常——由 Runner 的错误隔离层处理，
      绝不改变 Run 执行与权威状态；
    - 无需 observability 服务器或 Dashboard 即可使用。
    - 调用方拥有本地文件资源：使用 ``with JsonlTelemetrySink(path)``
      或在不再使用时显式调用幂等 ``close()``。
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._file = None
        self._closed = False

    @property
    def path(self) -> Path:
        """本 sink 写入的 JSONL 文件路径。"""
        return self._path

    def emit(self, event: TelemetryEvent) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("JsonlTelemetrySink is closed")
            if self._file is None:
                self._file = self._path.open("a", encoding="utf-8")
            if fcntl is not None:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX)
            try:
                self._file.write(event.model_dump_json() + "\n")
                self._file.flush()
            finally:
                if fcntl is not None:
                    fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)

    def flush(self) -> None:
        """Flush all writes accepted before this call.

        The method shares the write lock, so a caller can use it as a
        deterministic thread-level handoff point.  It deliberately does not
        reopen a never-used sink and remains harmless after :meth:`close`.
        """
        with self._lock:
            if self._file is not None:
                self._file.flush()

    def close(self) -> None:
        """Close the file permanently; repeated calls are safe.

        A closed sink never reopens.  This lets owners distinguish a late
        telemetry write (diagnostic failure, isolated by ``Runner``) from an
        accepted event, while preserving the final JSONL prefix intact.
        """
        with self._lock:
            if self._file is not None:
                self._file.close()
                self._file = None
            self._closed = True

    def __enter__(self) -> "JsonlTelemetrySink":
        """Return this sink; the context owns one deterministic close."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Close the local file even when the caller's Run fails."""
        self.close()
