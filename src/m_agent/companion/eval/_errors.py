"""Eval Companion 公共错误（ADR 0029）。

Eval 不参与 Runner 核心循环，也不给任何 Store 注入 hook；错误类型
只描述 Companion 边界上的确定性失败。所有错误继承
:class:`m_agent.runtime.MAgentError`，权威 RunStore/SessionStore 状态
在错误发生前保持不变。
"""

from __future__ import annotations

from ..._errors import MAgentError


class EvalError(MAgentError):
    """Eval Companion 公共错误基类。"""


class EvalSuiteError(EvalError):
    """Suite 展开失败：冻结声明之间不一致（如 Case Variant 未注册）。

    展开在任何 subject Run 创建之前 fail-closed。
    """


class EvalFixtureBoundaryError(EvalError):
    """EXECUTE fixture boundary 违例：访问了 fixture 未声明的外部系统。

    触发条件：非确定性（live 语义）Model Adapter，或 effect 不是
    READ_ONLY 且未在 Fixture Bundle 中声明的外部工具。抛出时尚未创建
    任何 subject Run、未发生任何模型 dispatch。
    """


class EvidenceIntegrityError(EvalError):
    """Evidence 来源被篡改或自相矛盾（index/内容不一致、行损坏）。

    Evidence 冲突保留为确定性失败，绝不静默降级为「证据不存在」。
    """


class EvalRecordConflictError(EvalError):
    """EvalStore 不可变身份冲突：同 id 异内容。

    EvalStore 只做 append-only：同内容重放幂等，异内容确定性拒绝，
    已保存事实不被改动。
    """
