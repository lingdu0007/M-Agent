"""可注入时钟：租约过期与接管测试的确定性时间源。

ADR 0013：租约期限必须成为可验证的持久
状态；过期与接管测试使用可注入 clock（:class:`FakeClock`）确定性
驱动，禁止依赖真实 sleep 的时间敏感断言。RunStore 持有唯一的时钟
实例，租约获取与每次权威 mutation 的有效期检查都使用该时钟的
``now()``——测试通过 :meth:`FakeClock.advance` 推进时间，而不是
等待真实时间流逝。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from ._steps import utc_now


class Clock(Protocol):
    """提供当前 UTC 时间的边界（生产与测试时钟的统一抽象）。"""

    def now(self) -> datetime: ...


class SystemClock:
    """生产时钟：返回真实 UTC 时间。"""

    def now(self) -> datetime:
        return utc_now()


class FakeClock:
    """确定性测试时钟：``now()`` 返回可手动推进/设置的时间。

    测试通过 :meth:`advance` 把时钟推进到租约过期之后，从而确定性
    地验证 takeover，而不是用真实 sleep 等待租约自然失效。
    """

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start if start is not None else utc_now()

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        """把当前时间推进 ``delta``（例如超过租约 TTL）。"""
        self._now = self._now + delta

    def set(self, value: datetime) -> None:
        self._now = value
