---
status: accepted
---

# Session 只保存成功 Run 的最终对话轮次

Agent Run 只有在 Run Store 权威地进入 `SUCCEEDED` 后，SessionRunner 才以冻结的 Session 历史版本和 `run_id` 向 Session Store 提交 Session Turn；Session Store 在一个原子操作中以 CAS 追加不可变 Turn、按 `run_id` 去重并清除 Session Run Claim。`REJECTED`、`FAILED` 与 `CANCELLED` 只清除 Claim，`WAITING` 不提交也不释放；Context Item、模型中间响应、工具调用与结果仍归 Run Store。该选择需要处理 Core 已成功但 Session 提交仍为 pending/conflict 的可见中间状态，却能在没有跨 Store 分布式事务的前提下通过幂等对账避免重复 Turn，并减少后续上下文中的冗余和敏感数据传播。
