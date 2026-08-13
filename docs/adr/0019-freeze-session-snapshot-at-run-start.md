---
status: accepted
---

# SessionRunner 在 Agent Run 创建前冻结 Session Snapshot

SessionRunner 在创建 Agent Run 前读取一次 Session Store，将带版本的 Session Snapshot 转换为 Core 的不可变 Conversation History，并显式交给 Runner 创建 Run；Core 把 Conversation History 保存为受保护 Run Payload，后续 Model Step 和恢复执行只复用该输入，不认识或重新读取 Session Store。成功结果在 Run 完成后由 SessionRunner 另行提交回 Session Store，这会使长时间运行的 Run 看不到后来新增的会话消息，也增加 Companion 的对账职责，但保持 Companion 到 Core 的单向依赖，并保证同一次执行在中断前后的上下文一致。
