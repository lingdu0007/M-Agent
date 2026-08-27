---
status: accepted
---

# 首版每个 Session 只允许一个非终态 Agent Run

同一 Session 在首版最多只能有一个处于 `CREATED`、`RUNNING` 或 `WAITING` 的 Agent Run，不同 Session 及无 Session 的 Run 仍可并发。SessionRunner 通过 Session Store 中持久化、原子建立的 Session Run Claim 强制该约束；Claim 不使用固定 TTL，而是以 Run Store 的权威状态对账，只有对应 Run 进入终态或确认从未创建后才能清理。该限制和跨 Store 幂等对账增加了 Companion 复杂度，但避免多个 Run 基于同一历史竞争提交、`WAITING` Run 因超时失去占用或消息顺序被静默覆盖；未来若有明确并行需求，应引入显式 Session branch。
