"""Conversation History：Run 创建时冻结的有序用户/助手消息。

ADR 0019 / CONTEXT.md：Conversation History 是 Agent Run 创建时显式
提供并冻结的有序用户/助手消息，是 Core 可持久化和恢复的模型输入。
它不包含 Session 身份、Session 版本或 Session Store 行为——Session
语义完全属于 Runtime Companion，Core 只消费这份冻结输入。
"""

from __future__ import annotations

import enum

from pydantic import BaseModel, ConfigDict


class ConversationRole(str, enum.Enum):
    """Conversation History 中允许的消息角色（仅用户与助手）。"""

    USER = "USER"
    ASSISTANT = "ASSISTANT"

    def __str__(self) -> str:  # pragma: no cover - 便捷展示
        return self.value


class ConversationMessage(BaseModel, frozen=True):
    """一条不可变的对话消息（内容不做额外解释，由上层语义决定）。"""

    model_config = ConfigDict(extra="forbid")

    role: ConversationRole
    content: str
