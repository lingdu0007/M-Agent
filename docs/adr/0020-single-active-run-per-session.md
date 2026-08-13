---
status: accepted
---

# 首版每个 Session 只允许一个非终态 Agent Run

同一 Session 在首版最多只能有一个处于 `CREATED`、`RUNNING` 或 `WAITING` 的 Agent Run，不同 Session 及无 Session 的 Run 仍可并发；当前 Run 进入终态后，下一个 Run 才能冻结新的 Session Snapshot。该限制牺牲同一对话内的并发请求能力，但避免多个 Run 基于同一历史竞争提交导致消息顺序不确定；未来若有明确需求，应引入显式 Session branch，而不是允许隐式覆盖。
