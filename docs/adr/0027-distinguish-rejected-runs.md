---
status: accepted
---

# 用 REJECTED 区分策略拒绝与执行失败

Agent Run 的终态扩展为 `SUCCEEDED`、`REJECTED`、`FAILED` 和 `CANCELLED`：Run Policy 返回 `REJECT` 时进入 `REJECTED`，只有本应继续的执行因不可恢复错误终止时才进入 `FAILED`。这一变更取代 ADR-0005 的六状态模型，使安全策略按设计拒绝的请求不会污染系统失败率和故障归因。
