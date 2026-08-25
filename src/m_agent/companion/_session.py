"""Session Companion 公共契约：SessionStore 端口与不可变数据模型。

ADR 0018-0021：

- Session 是应用拥有的连续对话边界，不是 Core 对象；Core 保持
  sessionless，只消费冻结的 Conversation History。
- 所有 SessionStore 读写都必须携带上层应用提供的 opaque
  :class:`SessionScope`；仅知道 Session identifier 不能跨 Scope 读取、
  claim、提交或推断其存在（fail-closed）。
- Session 历史使用从零开始的整数版本；Session Turn 不可变、有序、
  仅记录成功 Run 的最小对话事实。
- Session Run Claim 持久化且无固定 TTL：同一 Session 至多一个
  ``CREATED`` / ``RUNNING`` / ``WAITING`` 的 Agent Run，Claim 以权威
  RunStore 状态对账（见 ADR 0020）。
- 成功提交在单个原子操作中校验 claim、CAS version、以 ``run_id``
  append-once 去重、清除 claim 并递增版本；冲突保留 claim 且禁止
  自动 merge（ADR 0021）。

本模块只定义公开契约；InMemory 实现见
:mod:`m_agent.companion._in_memory_session_store`，组合行为见
:mod:`m_agent.companion._session_runner`。Session Payload 的独立保护
（Session PayloadCodec）由具体 Store 实现负责，本契约不涉及。
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from .._errors import MAgentError


class SessionError(MAgentError):
    """Session Companion 公共错误基类。"""


class SessionNotFoundError(SessionError):
    """目标 Session 在提供的 Scope 内不存在（或属于其他 Scope）。

    跨 Scope 访问与不存在不可区分：仅知道 Session identifier 不能
    推断其存在。权威状态未被改动。
    """


class DuplicateSessionError(SessionError):
    """同一 Scope 内 session_id 已存在；显式创建禁止覆盖。"""


class SessionClaimConflictError(SessionError):
    """Claim 冲突：Session 已有 active claim、claim 由其他 run identity
    持有、或该 run identity 已提交过 Turn。

    权威 claim 与历史均未被改动。
    """


class SessionVersionConflictError(SessionError):
    """Session 历史版本与期望不一致（CAS / exact-version 失败）。

    权威状态未被改动；失败方必须重新读取并显式决策，禁止自动 merge。
    """


class SessionScope(BaseModel, frozen=True):
    """上层应用随 SessionStore 操作提供的不透明授权与隔离上下文。

    应用把自身的授权材料（用户/租户组合、令牌摘要等）编码进
    ``token``；Store 从不解释其内容，只做相等性比较，从而限定一个
    ``session_id`` 可被谁读取或修改。它不是用户、租户或模型输入。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    token: str = Field(min_length=1)


class SessionRunClaim(BaseModel, frozen=True):
    """Session Store 中指向一个 Agent Run 的持久化占用声明（ADR 0020）。

    它在 Run 创建之前以一次原子操作绑定预分配 run identity 与冻结的
    Session 历史版本；它不是 Run Lease，也不代表进程所有权，没有
    固定 TTL——清理只能依据权威 RunStore 状态对账。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    run_id: str
    #: claim 建立时冻结的 Session 历史版本（提交时的 CAS 期望值）。
    session_version: int
    claimed_at: datetime


class SessionTurn(BaseModel, frozen=True):
    """一条不可变的成功对话轮次（ADR 0021）。

    只承载最小对话事实：稳定 turn/run 身份、精确定义身份与版本、
    文本用户输入、文本最终输出与时间。任意 metadata、多模态内容、
    system/tool 消息、中间模型输出、Context Item 与工具轨迹都不属于
    Session Turn（它们归 Run Store）。``run_id`` 是幂等键：同一
    run identity 的重复提交只产生这一条 Turn。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    turn_id: str
    session_id: str
    run_id: str
    definition_id: str
    definition_version: str
    user_input: str
    assistant_output: str
    created_at: datetime


class SessionSnapshot(BaseModel, frozen=True):
    """带版本的 Session 历史读取结果（完整或一页）。

    ``version`` 是读取时的权威历史版本（版本 == 已追加 Turn 数）；
    ``turns`` 按追加顺序排列，默认读取不截断、不替换、不总结——
    上下文选择是调用方的事，存储真相保持完整。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    version: int
    turns: tuple[SessionTurn, ...]
    #: 下一页起始 cursor（0-based Turn 序号）；None 表示没有更多。
    next_cursor: int | None


class SessionRecord(BaseModel, frozen=True):
    """Session 的可观测元数据视图（不含对话正文）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    version: int
    claim: SessionRunClaim | None
    created_at: datetime
    updated_at: datetime


class SessionCommitStatus(str, enum.Enum):
    """Session 提交状态：与 Core Run Status 分离且分别可观察。

    - ``NOT_READY``：Run 尚未进入权威 ``SUCCEEDED`` 终态，没有可
      提交的 Turn（含 RUNNING / WAITING 与非成功终态）。
    - ``COMMITTED``：Turn 已权威追加进历史。
    - ``PENDING``：Run 已成功但提交未完成（未尝试或 Store 暂不可用），
      以 run identity 幂等重试安全。
    - ``CONFLICT``：提交被拒绝（claim / 版本不匹配）或依据权威状态
      判定无法安全提交；claim 保留、禁止自动 merge。

    SessionStore 状态绝不改写 Core 终态：Core 成功 + 提交失败是可
    观察的中间状态，由应用显式对账。
    """

    NOT_READY = "NOT_READY"
    COMMITTED = "COMMITTED"
    PENDING = "PENDING"
    CONFLICT = "CONFLICT"

    def __str__(self) -> str:  # pragma: no cover - 便捷展示
        return self.value


class SessionCommitResult(BaseModel, frozen=True):
    """一次 Turn 提交尝试的结构化结果（SessionStore 层）。

    Store 只产生 ``COMMITTED``（新追加或按 run identity 幂等去重）
    或 ``CONFLICT``（claim / 版本校验失败，无任何 mutation）；
    ``NOT_READY`` / ``PENDING`` 是 SessionRunner 依据权威 Run 状态
    派生的可观察状态。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: SessionCommitStatus
    turn: SessionTurn | None
    version: int


@runtime_checkable
class SessionStore(Protocol):
    """Session Store 行为契约：Scope 隔离、版本化历史、原子 Claim 与 CAS 提交。

    所有操作都要求 :class:`SessionScope`；跨 Scope 访问与"不存在"
    表现一致（fail-closed）。历史版本从零开始，每次成功提交恰好 +1；
    Turn 不可变、追加有序，以 ``run_id`` 幂等去重。InMemory 与 SQLite
    实现共享同一份行为契约。
    """

    async def create_session(
        self, scope: SessionScope, session_id: str
    ) -> SessionRecord:
        """在 Scope 内显式创建 Session：初始版本为零、历史为空、无 claim。

        重复创建抛 :class:`DuplicateSessionError`。
        """
        ...

    async def get_session(
        self, scope: SessionScope, session_id: str
    ) -> SessionRecord | None:
        """读取 Session 元数据（含当前 claim）；Scope 内不存在返回 None。"""
        ...

    async def read_snapshot(
        self,
        scope: SessionScope,
        session_id: str,
        *,
        expected_version: int | None = None,
        after: int = 0,
        limit: int | None = None,
    ) -> SessionSnapshot:
        """读取带版本的完整历史（默认）或一页有序 Turn。

        :param expected_version: 提供时执行 exact-version 读取：权威
            版本不一致抛 :class:`SessionVersionConflictError`，绝不
            静默返回其他版本。
        :param after: cursor（0-based Turn 序号），从该位置起读。
        :param limit: 页大小；None 返回剩余全部历史（无静默截断）。
        """
        ...

    async def get_claim(
        self, scope: SessionScope, session_id: str
    ) -> SessionRunClaim | None:
        """当前 active claim；无则 None。"""
        ...

    async def claim_run(
        self,
        scope: SessionScope,
        session_id: str,
        run_id: str,
        *,
        expected_version: int,
    ) -> SessionRunClaim:
        """以一次原子操作建立 Session Run Claim。

        绑定预分配 ``run_id`` 与冻结的当前历史版本（必须等于
        ``expected_version``）。Session 已有 active claim 时抛
        :class:`SessionClaimConflictError`（权威 claim 不变）；版本
        不一致抛 :class:`SessionVersionConflictError`；该 run identity
        已提交过 Turn 时抛 :class:`SessionClaimConflictError`
        （fail-closed，防止为已完成 Run 制造永久占用）。
        """
        ...

    async def release_claim(
        self, scope: SessionScope, session_id: str, run_id: str
    ) -> None:
        """释放当前 claim；只有 claim 持有的 run identity 可以释放。

        claim 由其他 run 持有时抛 :class:`SessionClaimConflictError`；
        无 claim 时是稳定 no-op（崩溃恢复的幂等重放）。
        """
        ...

    async def commit_turn(
        self,
        scope: SessionScope,
        session_id: str,
        turn: SessionTurn,
        *,
        expected_version: int,
    ) -> SessionCommitResult:
        """在单个原子操作中提交一条成功 Turn。

        校验 claim（``turn.run_id`` 必须是当前 claim 持有者）→ CAS
        历史版本 → 以 ``run_id`` append-once 追加 Turn → 清除 claim →
        版本 +1。同一 run identity 重复提交幂等去重（返回既有 Turn，
        无任何 mutation）；claim 不匹配或版本不一致返回
        ``CONFLICT``（claim 保留、历史不变、禁止自动 merge）。
        """
        ...

    async def find_turn_by_run(
        self, scope: SessionScope, session_id: str, run_id: str
    ) -> SessionTurn | None:
        """按 run identity 查询已提交 Turn（幂等键查询）；无则 None。"""
        ...
