"""Model Adapter 契约与确定性实现。

ADR 0030：Model Adapter 必须声明 streaming、tool calling、原生
structured output 与 usage reporting 等 Model Capabilities；
Definition 声明所需能力，注册时校验兼容性，禁止静默降级。

Live 与 deterministic（fake）Adapter 是明显不同的类型：
- :class:`ModelAdapter` 是 live provider 适配器的基类（deterministic=False）；
- :class:`DeterministicModelAdapter` 是测试与演示用的确定性 fake
  （deterministic=True），二者在任何场景都不会混淆。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence

from pydantic import BaseModel, Field

from ._context import ContextItem
from ._tools import ToolCall, ToolOutcome, ToolSpec

_CAPABILITY_FIELDS = (
    "streaming",
    "tool_calling",
    "structured_output",
    "usage_reporting",
)


class ModelCapabilities(BaseModel, frozen=True):
    """Model Adapter 对语义支持的明确声明。"""

    streaming: bool = False
    tool_calling: bool = False
    structured_output: bool = False
    usage_reporting: bool = False

    def supports(self, required: "ModelCapabilities") -> bool:
        """required 中每个声明为 True 的能力都必须被本声明覆盖。"""
        return all(
            not getattr(required, name) or getattr(self, name)
            for name in _CAPABILITY_FIELDS
        )


class ModelUsage(BaseModel, frozen=True):
    """模型调用用量（仅当 Adapter 声明 usage_reporting 时提供）。"""

    input_tokens: int | None = None
    output_tokens: int | None = None


class ModelRequest(BaseModel, frozen=True):
    """一次模型请求。

    - ``instructions``：来自 Definition 的 Agent Instruction（受信）；
    - ``context_items``：外部上下文，作为数据交付（ADR 0017）。
    - ``tools``：模型可调用的工具声明（含参数 Schema）；
    - ``tool_outcomes``：此前 Tool Step 的显式结果，作为外部数据
      交付（ADR 0017 / ADR 0024）。Tool Outcome 与 Context Item 一样
      永不写入或替换 ``instructions``，Model Adapter 必须保留这一
      边界。
    """

    input: str
    instructions: str
    context_items: tuple[ContextItem, ...] = Field(default_factory=tuple)
    tools: tuple[ToolSpec, ...] = Field(default_factory=tuple)
    tool_outcomes: tuple[ToolOutcome, ...] = Field(default_factory=tuple)


class ModelDelta(BaseModel, frozen=True):
    """一次模型流式输出增量（Ticket 08 / ADR 0011）。

    增量只作为带 ``attempt_id`` 的 Run Update 发布（:class:`RunUpdate`
    的 ``MODEL_DELTA``），**绝不写入 Run Store、不构成 checkpoint**；
    只有流结束产出的完整 :class:`ModelResponse` 才由 Runner checkpoint。
    失败重试时新的 Step Attempt 拥有新 ``attempt_id``，消费者据此替换
    废弃尝试的部分输出，而不是把不同尝试的文本拼接起来。
    """

    content: str


class ModelResponse(BaseModel, frozen=True):
    """一次模型请求的完整响应。

    - ``content``：文本内容；当响应只请求工具时可以为 None；
    - ``tool_calls``：模型请求的工具调用（0..N 个）；同一响应内的
      多个调用分别形成各自的 Tool Step，由 Runner 顺序执行
      （ADR 0004）。
    - ``usage``：用量（仅当 Adapter 声明 usage_reporting 时提供）。
    """

    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = Field(default_factory=tuple)
    usage: ModelUsage | None = None


class ModelAdapter(ABC):
    """Live provider Model Adapter 基类 / 扩展边界。

    `deterministic` 恒为 False：任何基于本类的实现都不是确定性 fake。
    """

    capabilities: ModelCapabilities

    #: 确定性 fake 的可见标记；live adapter 恒为 False。
    deterministic: bool = False

    def definition_contract_fingerprint(self) -> str:
        """Return stable, non-secret configuration identity for a Run Snapshot.

        Adapters whose behavior depends on configuration beyond capabilities
        must override this method. The default deliberately has no fingerprint:
        arbitrary application adapters may be imported under different module
        names across processes, so only adapters with a stable configuration
        representation can opt into recovery comparison.
        """
        return ""

    @abstractmethod
    async def generate(self, request: ModelRequest) -> ModelResponse:
        """按统一模型契约生成一次完整响应。"""

    async def stream(
        self, request: ModelRequest,
    ) -> AsyncIterator[ModelDelta | ModelResponse]:
        """流式生成一次模型响应（Ticket 08 / ADR 0011）。

        只有声明 ``capabilities.streaming=True`` 的 Adapter 才会被
        Runner 调用本方法（ADR 0030：不静默降级）。契约：

        - 逐个产出 :class:`ModelDelta`（文本增量）；
        - 流的**最后一个元素必须是完整的 :class:`ModelResponse`**
          （可携带 tool_calls / usage），表示本次调用成功结束；
        - 中途失败应抛出结构化 :class:`StepFailure`（如
          :class:`ModelFailure`），由 Runner 记录失败 Step Attempt 并按
          Retry Policy 决定是否以新 ``attempt_id`` 重试。

        未覆写（默认抛 NotImplementedError）意味着该 Adapter 不支持
        流式；Runner 只在 `streaming=True` 时调用，未满足即显式失败。
        """
        raise NotImplementedError(
            f"{type(self).__name__} declares streaming=True but does not "
            "implement stream()"
        )


class DeterministicModelAdapter(ModelAdapter):
    """确定性 fake Model Adapter，用于测试、演示与离线示例。

    与 live :class:`ModelAdapter` 类型和配置均明确区分：`deterministic`
    恒为 True，响应由构造参数决定，不访问任何网络或供应商。
    """

    deterministic: bool = True

    def __init__(
        self,
        responses: Sequence[str] = ("deterministic response",),
        capabilities: ModelCapabilities | None = None,
    ) -> None:
        if not responses:
            raise ValueError("responses must contain at least one string")
        self._responses: tuple[str, ...] = tuple(responses)
        self.capabilities: ModelCapabilities = capabilities or ModelCapabilities()
        self.call_count: int = 0
        self._last_request: ModelRequest | None = None

    @property
    def last_request(self) -> ModelRequest | None:
        """最近一次请求，供测试断言（如验证 create 之前无模型调用）。"""
        return self._last_request

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        self._last_request = request
        index = min(self.call_count - 1, len(self._responses) - 1)
        return ModelResponse(content=self._responses[index])


class DeterministicStreamingModelAdapter(DeterministicModelAdapter):
    """确定性流式 fake Model Adapter（Ticket 08 / ADR 0011）。

    与 live :class:`ModelAdapter` 和普通 :class:`DeterministicModelAdapter`
    明确区分：`deterministic` 恒为 True，`capabilities.streaming` 默认
    True，行为完全由构造参数决定，不访问任何网络或供应商。

    :meth:`stream` 按 ``chunks`` 逐个产出 :class:`ModelDelta`，最后产出
    携带 ``content="".join(chunks)`` 的完整 :class:`ModelResponse`；
    传入 ``tool_calls`` 时完整响应会请求这些工具（需要
    ``capabilities.tool_calling``，构造时自动补齐）。

    需要"部分输出后失败再成功"的测试通过子类覆写 :meth:`stream` 注入
    结构化失败（参考 tests/test_m_agent_stream_cancel.py）。
    """

    def __init__(
        self,
        chunks: Sequence[str] = ("Hel", "lo ", "world"),
        *,
        tool_calls: Sequence[ToolCall] = (),
        capabilities: ModelCapabilities | None = None,
    ) -> None:
        if not chunks:
            raise ValueError("chunks must contain at least one string")
        caps = capabilities or ModelCapabilities(streaming=True)
        if tool_calls and not caps.tool_calling:
            caps = ModelCapabilities(
                streaming=True,
                tool_calling=True,
                structured_output=caps.structured_output,
                usage_reporting=caps.usage_reporting,
            )
        super().__init__(responses=("",), capabilities=caps)
        self._chunks: tuple[str, ...] = tuple(chunks)
        self._tool_calls: tuple[ToolCall, ...] = tuple(tool_calls)
        self.requests: list[ModelRequest] = []

    async def stream(
        self, request: ModelRequest,
    ) -> AsyncIterator[ModelDelta | ModelResponse]:
        self.call_count += 1
        self._last_request = request
        self.requests.append(request)
        for chunk in self._chunks:
            yield ModelDelta(content=chunk)
        yield ModelResponse(
            content="".join(self._chunks),
            tool_calls=self._tool_calls,
        )


def serialize_model_response(response: ModelResponse) -> str:
    """把完整 ModelResponse 序列化为 Checkpoint 输出（含 tool_calls）。

    运行时内部使用（Runner 写入 Model Step Checkpoint）；恢复时经
    :func:`deserialize_model_response` 还原，以判断该响应是否请求了
    工具以及是否已产生最终内容（Ticket 05：checkpoint 必须携带完整
    响应，恢复才能精确重建执行位置）。
    """
    return response.model_dump_json()


def deserialize_model_response(payload: str) -> ModelResponse:
    """从 Model Step Checkpoint 输出还原完整 ModelResponse。"""
    return ModelResponse.model_validate_json(payload)
