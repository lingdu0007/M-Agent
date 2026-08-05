---
status: accepted
---

# Agent Run 首次启动时冻结 Session Snapshot

Agent Run 首次从 `CREATED` 进入 `RUNNING` 时读取一次 Session Store，并把对话历史及版本作为不可变 Session Snapshot 保存到 Run Store；后续 Model Step 和恢复执行复用该快照，不重新读取已经变化的 Session。成功结果在 Run 完成后另行提交回 Session Store，这会使长时间运行的 Run 看不到后来新增的会话消息，但保证同一次执行在中断前后的上下文一致。
