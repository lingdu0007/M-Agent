"""pytest 根配置（Ticket 10 live 契约测试的 opt-in 门控）。

默认离线原则（PRD「Provider contract seam」）：live 契约测试
（``@pytest.mark.live``）**永不进入默认 CI / 默认 pytest 运行**——
没有显式 ``-m live`` 时，本 hook 给它们打上 skip（原因明确：未
opt-in），因此默认运行不发出任何网络请求，但报告仍显示这些测试
存在且被跳过（区别于"缺失凭证"的 skip）。

显式 ``pytest -m live`` 时放行；测试自身的 ``setUp`` 仍要求
``M_AGENT_RUN_LIVE_TESTS=1``。缺少该开关以 ``OPTED_OUT`` 跳过；已开启
但缺少凭证以 ``MISSING_CREDENTIALS`` 跳过。两者均不发网络请求。

说明：不用 ``addopts = ["-m", "not live"]``，因为 pytest 会把
addopts 与命令行 ``-m`` 按 AND 合并，导致 ``-m live`` 无法覆盖默认
排除。
"""

from __future__ import annotations

import re

import pytest


def _explicitly_opted_in(config) -> bool:
    """命令行 / ini 的 ``-m`` 表达式中包含正的 ``live`` 项。"""
    markexpr = (config.getoption("-m") or "").strip()
    return "live" in markexpr.split() and re.search(
        r"\bnot\s+live\b", markexpr
    ) is None


def pytest_collection_modifyitems(config, items) -> None:
    if _explicitly_opted_in(config):
        return
    for item in items:
        if item.get_closest_marker("live") is not None:
            item.add_marker(
                pytest.mark.skip(
                    reason=(
                        "OPTED_OUT: live contract tests require "
                        "M_AGENT_RUN_LIVE_TESTS=1 and `pytest -m live`; "
                        "no provider request was made"
                    )
                )
            )
