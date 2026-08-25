"""Run Update 契约（ADR 0010 / ADR 0011）。

CONTEXT.md：Run Update 是 Runner 面向上层应用发布的稳定实时通知，
用于呈现 Agent Run 的状态、步骤和输出进展；它不是权威状态，丢失后
由应用从 Run Store 重新读取当前事实。

本模块定义唯一的公开 Run Update 数据契约：

- 订阅只通过公开 Runner API（:meth:`Runner.subscribe_run`），订阅者
  无需访问任何内部执行对象；
- 模型流式增量（``MODEL_DELTA``）携带 ``run_id`` / ``step_id`` /
  ``attempt_id``（ADR 0011 / AC 2），消费者按 Attempt 渲染、
  替换或丢弃部分输出；
- 增量（delta）**不是 checkpoint**：只有完整模型响应持久化后才发布
  ``STEP_COMPLETED``（ADR 0011 / AC 3-4），断线后的权威事实必须从
  RunStore 查询（AC 7，本契约不承诺持久回放，ADR 0010）；
- 订阅者断开或处理失败只移除订阅，绝不改变 Run 执行或权威状态
  （AC 6）。
"""

from __future__ import annotations

import enum
from datetime import datetime

from pydantic import BaseModel, Field

from ._status import RunStatus
from ._steps import StepType, utc_now


class RunUpdateType(str, enum.Enum):
    """Run Update 的类型（机器可读，稳定标识符）。"""

    #: Run Status 发生转换（RUNNING / WAITING / 终态等）。
    STATUS_CHANGED = "STATUS_CHANGED"
    #: 一个新的 Step Attempt 开始（携带 step_id / attempt_id / step_type）。
    #: 消费者可用 attempt_id 重置该 Step 的部分输出渲染。
    STEP_STARTED = "STEP_STARTED"
    #: 模型流式增量（携带 step_id / attempt_id 与本次增量文本）。
    #: 非权威：不写入 Run Store，不构成 checkpoint。
    MODEL_DELTA = "MODEL_DELTA"
    #: 一个 Step Attempt 失败（携带 step_id / attempt_id / step_type）。
    #: 后续重试会以新的 attempt_id 重新发布 STEP_STARTED / MODEL_DELTA。
    ATTEMPT_FAILED = "ATTEMPT_FAILED"
    #: 一个 Step 完成并已 checkpoint（携带 step_id / attempt_id /
    #: step_type）：只有此时该 Step 的结果才成为权威恢复点。
    STEP_COMPLETED = "STEP_COMPLETED"


class RunUpdate(BaseModel, frozen=True):
    """一条稳定、非权威的 Run 实时通知（ADR 0010）。

    所有字段均为纯数据，可由上层应用直接序列化到自己的传输层
    （SSE / WebSocket / 终端渲染由应用决定，运行时不做传输）。
    """

    run_id: str
    update_type: RunUpdateType
    #: 关联的 Run Step；非 Step 级事件（STATUS_CHANGED）为 None。
    step_id: str | None = None
    #: 关联的 Step Attempt；``MODEL_DELTA`` / ``STEP_STARTED`` /
    #: ``ATTEMPT_FAILED`` / ``STEP_COMPLETED`` 必填。
    attempt_id: str | None = None
    step_type: StepType | None = None
    #: ``STATUS_CHANGED`` 时的新 Run Status。
    status: RunStatus | None = None
    #: ``MODEL_DELTA`` 时的本次增量文本；其他类型为 None。
    content: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
