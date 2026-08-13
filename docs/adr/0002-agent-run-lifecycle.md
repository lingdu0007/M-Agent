---
status: accepted
---

# 以 Agent Run 作为核心生命周期对象

M-Agent 将可复用且不承载单次执行状态的 Agent Definition，与针对一次输入产生的 Agent Run 分开；运行状态、步骤、用量、checkpoint 和结果均归属于 Agent Run，Session 只负责关联共享连续对话上下文的多次 Agent Run。相比继续以同步 `Agent.run()` 调用栈作为隐式生命周期，这一模型增加了显式状态管理成本，但为并发隔离、取消、恢复和可观测性提供了稳定归属边界。
