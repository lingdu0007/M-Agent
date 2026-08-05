---
status: superseded by ADR-0027
---

# Agent Run 使用六状态生命周期

Agent Run 的 Run Status 采用 `CREATED`、`RUNNING`、`WAITING`、`SUCCEEDED`、`FAILED` 和 `CANCELLED`，其中 `WAITING` 统一表示等待重试时间、人工审批或其他外部信号，后三者为终态。进程崩溃本身不产生新的 Run Status，也不直接把 Run 标记为 `FAILED`；恢复机制根据持久化的 Run Step 判断并接管未完成执行，从而区分运行载体故障与 Agent Run 的业务结果。
