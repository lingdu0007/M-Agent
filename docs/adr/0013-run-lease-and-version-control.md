---
status: accepted
---

# 使用 Run Lease 防止重复推进

Runner 在推进非终态 Agent Run 前必须从 Run Store 取得带有效期的排他 Run Lease，状态写入同时校验租约持有者与记录版本；Runner 失去租约后停止推进，租约过期后其他 Runner 才能接管。该机制增加了续约与冲突处理复杂度，但防止重启、重复请求或多进程部署并发执行同一 Run；它只保护单个 Run，不负责任务发现或 worker 调度。
