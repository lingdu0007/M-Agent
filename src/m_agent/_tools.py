"""Tool 契约与确定性实现（Ticket 05）。

CONTEXT.md / ADR 0007 / ADR 0024：

- **Tool Effect**：每个工具声明 ``READ_ONLY``、``IDEMPOTENT`` 或
  ``NON_IDEMPOTENT`` 的重试安全性；未声明时按 ``NON_IDEMPOTENT``
  处理（fail-closed，ADR 0007）。Runner 只在 Retry Policy 与失败
  分类（Ticket 06 / ADR 0025）同时允许时自动重试；UNCERTAIN +
  NON_IDEMPOTENT 绝不自动重放。
- **Tool Outcome**：工具执行只以显式 ``SUCCESS(result)`` 与
  ``REJECTED(code, message)`` 结束；业务拒绝（REJECTED）是正常完成，
  不是基础设施失败。未捕获异常属于 Step Attempt 失败，绝不包装成
  自然语言结果交给模型（ADR 0024）。
- **Side-effecting Tool**：可能改变运行时之外状态的工具必须声明
  幂等能力；本模块的 effect 声明就是这个边界。
- **live / deterministic 区分**：与 ModelAdapter / ContextProvider
  同一原则——:class:`Tool` 是 live 工具基类（deterministic=False），
  :class:`DeterministicTool` 是测试与演示用的确定性 fake
  （deterministic=True）。

Tool Outcome 作为外部数据进入模型上下文（ADR 0017），运行时把
outcome 序列化进 Tool Step 的 Attempt 与 Checkpoint（Payload 区，
经 Payload Codec 保护，ADR 0033）。
"""

from __future__ import annotations

import enum
import inspect
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field, model_validator

#: 工具参数 JSON Schema 的默认形状（无参数工具）。
DEFAULT_TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
}


class ToolEffect(str, enum.Enum):
    """工具对外部状态影响及其重试安全性的声明（ADR 0007）。

    - ``READ_ONLY``：只读，不改变运行时之外的状态；
    - ``IDEMPOTENT``：重复执行产生相同外部效果；
    - ``NON_IDEMPOTENT``：重复执行可能产生不同外部效果；
      未声明时按 ``NON_IDEMPOTENT`` 处理（fail-closed）。
    """

    READ_ONLY = "READ_ONLY"
    IDEMPOTENT = "IDEMPOTENT"
    NON_IDEMPOTENT = "NON_IDEMPOTENT"


class ToolCall(BaseModel, frozen=True):
    """模型在一次响应中请求的一次工具调用。

    ``call_id`` 是模型响应内的稳定标识，用于把 Tool Outcome 与
    对应的调用关联；``arguments`` 是 JSON 编码的参数对象。
    """

    call_id: str
    tool_name: str
    arguments: str


class ToolOutcomeStatus(str, enum.Enum):
    """Tool Outcome 的显式完成状态（ADR 0024）。

    SUCCESS 与 REJECTED 都是正常完成的显式结果；未捕获异常不属于
    Tool Outcome。
    """

    SUCCESS = "SUCCESS"
    REJECTED = "REJECTED"


class ToolOutcome(BaseModel, frozen=True):
    """Tool Step 明确返回的 ``SUCCESS`` 或 ``REJECTED`` 结构化结果。

    - ``SUCCESS``：携带 ``result``（工具结果文本，作为外部数据）；
    - ``REJECTED``：携带 ``code``（机器可读业务码）与 ``message``
      （业务拒绝说明）；业务拒绝是正常完成，不是执行失败。

    Outcome 始终作为外部数据进入模型上下文，绝不写入或替换
    Agent Instruction（ADR 0017）。
    """

    status: ToolOutcomeStatus
    call_id: str
    tool_name: str
    result: str | None = None
    code: str | None = None
    message: str | None = None

    @model_validator(mode="after")
    def _validate_payload_shape(self) -> ToolOutcome:
        """Keep SUCCESS and REJECTED as distinct structured data shapes."""
        if self.status is ToolOutcomeStatus.SUCCESS:
            if self.result is None:
                raise ValueError("SUCCESS ToolOutcome requires result")
            if self.code is not None or self.message is not None:
                raise ValueError(
                    "SUCCESS ToolOutcome cannot include rejection fields"
                )
            return self
        if self.result is not None:
            raise ValueError("REJECTED ToolOutcome cannot include result")
        if self.code is None or self.message is None:
            raise ValueError(
                "REJECTED ToolOutcome requires code and message"
            )
        return self

    @classmethod
    def success(
        cls, call_id: str, tool_name: str, result: str
    ) -> ToolOutcome:
        """构造一个 SUCCESS outcome。"""
        return cls(
            status=ToolOutcomeStatus.SUCCESS,
            call_id=call_id,
            tool_name=tool_name,
            result=result,
        )

    @classmethod
    def rejected(
        cls, call_id: str, tool_name: str, code: str, message: str
    ) -> ToolOutcome:
        """构造一个 REJECTED outcome（业务拒绝，正常完成）。"""
        return cls(
            status=ToolOutcomeStatus.REJECTED,
            call_id=call_id,
            tool_name=tool_name,
            code=code,
            message=message,
        )


class ToolRequest(BaseModel, frozen=True):
    """一次工具调用的输入。

    本版本只有模型传来的 ``call_id``、``tool_name`` 与 JSON 编码的
    ``arguments``；工具实现自行解析参数并决定返回 SUCCESS 或
    REJECTED，或抛出异常（异常 = 失败的 Step Attempt）。
    """

    call_id: str
    tool_name: str
    arguments: str


class ToolSpec(BaseModel, frozen=True):
    """交付给 Model Adapter 的工具声明（含参数 JSON Schema）。

    模型据此生成工具调用请求；effect 是运行时恢复语义的声明。
    """

    name: str
    description: str = ""
    effect: ToolEffect
    parameters: dict[str, Any] = Field(
        default_factory=lambda: dict(DEFAULT_TOOL_PARAMETERS)
    )


class ToolDeclaration(BaseModel, frozen=True):
    """Definition Snapshot 中持久化的工具能力标识（ADR 0022/0023）。

    只记录 ``name`` 与 ``effect``，不序列化工具实现（callable 不进入
    Run Store）；恢复行为由 Run 启动时冻结的声明决定。
    """

    name: str
    effect: ToolEffect


class Tool(ABC):
    """Live 工具扩展边界 / 基类。

    `deterministic` 恒为 False：任何基于本类的实现都不是确定性 fake。
    实现方在 :meth:`invoke` 中执行外部副作用，并显式返回
    :class:`ToolOutcome`；意外异常不得作为 outcome 返回给模型。
    """

    #: 确定性 fake 的可见标记；live tool 恒为 False。
    deterministic: bool = False

    def __init__(
        self,
        *,
        name: str,
        description: str = "",
        effect: ToolEffect = ToolEffect.NON_IDEMPOTENT,
        parameters: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.description = description
        #: Tool Effect（ADR 0007）：未声明时按 NON_IDEMPOTENT fail-closed。
        self.effect = effect
        self.parameters = (
            parameters if parameters is not None else dict(DEFAULT_TOOL_PARAMETERS)
        )

    @abstractmethod
    async def invoke(self, request: ToolRequest) -> ToolOutcome:
        """执行一次工具调用并显式返回 SUCCESS 或 REJECTED outcome。

        未捕获异常属于 Step Attempt 失败（由 Runner 记录为失败
        Attempt 并停止 Run），不会被转换为模型可见的工具结果。
        """

    def spec(self) -> ToolSpec:
        """生成交付给 Model Adapter 的工具声明。"""
        return ToolSpec(
            name=self.name,
            description=self.description,
            effect=self.effect,
            parameters=self.parameters,
        )


class DeterministicTool(Tool):
    """确定性 fake Tool，用于测试、演示与离线示例。

    与 live :class:`Tool` 类型和标记明确区分：`deterministic` 恒为
    True，行为由构造参数或子类覆写决定，不访问任何网络或外部服务。
    构造时传入的 ``handler`` 接收 :class:`ToolRequest`，返回
    :class:`ToolOutcome`（或抛异常模拟失败）。
    """

    deterministic: bool = True

    def __init__(
        self,
        *,
        name: str,
        description: str = "",
        effect: ToolEffect = ToolEffect.NON_IDEMPOTENT,
        parameters: dict[str, Any] | None = None,
        handler: Callable[[ToolRequest], Any] | None = None,
    ) -> None:
        super().__init__(
            name=name,
            description=description,
            effect=effect,
            parameters=parameters,
        )
        self._handler = handler

    async def invoke(self, request: ToolRequest) -> ToolOutcome:
        if self._handler is None:
            raise NotImplementedError(
                f"tool {self.name} does not define a handler"
            )
        result = self._handler(request)
        if inspect.isawaitable(result):
            result = await result
        return result

def serialize_tool_outcome(outcome: ToolOutcome) -> str:
    """把 Tool Outcome 序列化为 Attempt / Checkpoint 的持久化输出。

    运行时内部使用（Runner 写入 Checkpoint），恢复时经
    :func:`deserialize_tool_outcome` 还原。
    """
    return outcome.model_dump_json()


def deserialize_tool_outcome(payload: str) -> ToolOutcome:
    """从 Checkpoint / Attempt 输出还原 Tool Outcome。"""
    return ToolOutcome.model_validate_json(payload)
