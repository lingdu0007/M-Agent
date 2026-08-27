"""Run Payload Codec 边界（ADR 0033）。

Run Store 把可查询的 Run Metadata 与包含模型内容、Context Item、
工具参数及结果的 Run Payload 分离；Run Payload 只能经上层应用显式
配置的 :class:`PayloadCodec` 后持久化，任何存取路径都不绕过 Codec。

- :class:`PayloadCodec` 是序列化与保护边界，不接收 API Key、访问令牌
  等运行凭据；凭据不是合法的 Payload 数据。
- :class:`PlaintextPayloadCodec` 只用于本地开发与测试，必须在构造
  RunStore 时显式选择；它不宣称提供静态加密或任何保护。

生产集成必须选择符合自身安全要求的受保护 Codec（security extras，
本版本 范围外）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class PayloadCodec(ABC):
    """Run Payload 的序列化与保护边界。

    上层应用为 Run Store 配置一个 Codec；RunStore 用它对 Payload
    做 encode/decode 往返。实现不得假设 Payload 内容可信，也不得
    接收或持久化运行凭据。
    """

    #: 描述性名称，用于日志与诊断（如 "plaintext-dev"）。
    name: str = "payload-codec"

    @abstractmethod
    def encode(self, payload: str) -> bytes:
        """把一段 Payload 编码为可持久化的字节。"""

    @abstractmethod
    def decode(self, encoded: bytes) -> str:
        """把持久化的字节解码回 Payload；无法识别时抛 ValueError。"""


class PlaintextPayloadCodec(PayloadCodec):
    """显式的开发/测试用明文 Codec。

    必须由调用方显式选择（``SQLiteRunStore(path, payload_codec=
    PlaintextPayloadCodec())``），不是生产默认。编码结果带
    ``m-agent-plaintext:`` 前缀标记，便于测试断言持久化字节确实
    经过 Codec 处理，也便于拒绝用错误 Codec 读取的数据。

    .. warning::
        明文编码不提供任何保密性，绝不可用于含敏感内容的真实负载；
        它只用于确定性测试与本地实验。
    """

    name: str = "plaintext-dev"

    _PREFIX = b"m-agent-plaintext:"

    def encode(self, payload: str) -> bytes:
        return self._PREFIX + payload.encode("utf-8")

    def decode(self, encoded: bytes) -> str:
        if not encoded.startswith(self._PREFIX):
            raise ValueError(
                "encoded payload does not start with "
                f"{self._PREFIX!r}; was it written by another codec?"
            )
        return encoded[len(self._PREFIX) :].decode("utf-8")
