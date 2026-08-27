"""Context Provider 契约与确定性实现。

ADR 0014 / CONTEXT.md：Context Provider 是应用在 Model Step 前
确定性选择的通用外部上下文扩展能力，不暴露或拥有其背后的检索、
存储或生成机制；核心运行时不定义 Retriever，也不拥有 RAG 内部
能力（ADR 0014）。

ADR 0016：Provider 不返回失去来源信息的裸文本，而是返回带 Run 内
稳定 ``item_id``、``content``、``source`` 与提供方 ``metadata`` 的
:class:`ContextItem`；Runner 保留顺序与溯源信息，但不解释检索分数、
不执行 rerank，也不决定最终引用格式。

ADR 0017：Context Item 始终作为外部数据进入模型上下文，不能写入
或提升为 Agent Instruction；只有 Agent Definition 与上层应用的受信
控制接口可以提供 Agent Instruction。

Live 与 deterministic（fake）Provider 是明显不同的类型（与
ModelAdapter 同一原则）：
- :class:`ContextProvider` 是 live provider 的基类（deterministic=False）；
- :class:`DeterministicContextProvider` 是测试与演示用的确定性 fake
  （deterministic=True）。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, Field


class ContextItem(BaseModel, frozen=True):
    """Context Provider 返回的一条结构化外部数据（ADR 0016）。

    - ``item_id``：Run 内稳定标识；
    - ``content``：外部内容文本；
    - ``source``：来源标识；
    - ``metadata``：提供方元数据（如来源文档标题、检索分数等）。
      运行时保留但不解释这些元数据。

    外部内容始终是不可信数据（ADR 0017）：它可能包含指令注入文本，
    运行时必须把它作为数据交付，绝不提升为 Agent Instruction。
    """

    item_id: str
    content: str
    source: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContextRequest(BaseModel, frozen=True):
    """一次 Context Provider 调用的输入。

    本版本只有 Run 输入；Session 历史等输入由后续版本 的 Session
    Snapshot 提供，不在本版本 范围。
    """

    input: str


class ContextProvider(ABC):
    """Live 外部上下文提供器的扩展边界 / 基类。

    `deterministic` 恒为 False：任何基于本类的实现都不是确定性 fake。
    实现方负责把背后的检索、存储或生成机制收敛在本边界之内。
    """

    #: 确定性 fake 的可见标记；live provider 恒为 False。
    deterministic: bool = False

    @abstractmethod
    async def provide(self, request: ContextRequest) -> Sequence[ContextItem]:
        """在依赖它的 Model Step 之前确定性提供外部上下文。

        返回的 Context Item 必须带 ``item_id`` / ``content`` /
        ``source`` 与提供方 ``metadata``（ADR 0016）。实现方不得
        期望返回值被解释为指令或用于替换 Agent Instruction。
        """


class DeterministicContextProvider(ContextProvider):
    """确定性 fake Context Provider，用于测试、演示与离线示例。

    与 live :class:`ContextProvider` 类型和标记明确区分：
    `deterministic` 恒为 True，返回内容由构造参数决定，不访问任何
    网络或外部服务。测试子类可覆盖 :meth:`provide` 记录调用次数、
    模拟外部数据变化或注入故障。
    """

    deterministic: bool = True

    def __init__(self, items: Sequence[ContextItem] = ()) -> None:
        self._items: tuple[ContextItem, ...] = tuple(items)
        self.call_count: int = 0

    async def provide(self, request: ContextRequest) -> Sequence[ContextItem]:
        self.call_count += 1
        return list(self._items)


def serialize_context_items(items: Sequence[ContextItem]) -> str:
    """把 Context Items 序列化为 Checkpoint / Attempt 的持久化输出。

    运行时内部使用（Runner 写入 Checkpoint），非公共 API。
    """
    return json.dumps(
        [item.model_dump(mode="json") for item in items], ensure_ascii=False
    )


def deserialize_context_items(payload: str) -> list[ContextItem]:
    """从 Checkpoint / Attempt 输出还原 Context Items。

    运行时内部使用（Runner 恢复时复用），非公共 API。
    """
    return [ContextItem.model_validate(obj) for obj in json.loads(payload)]
