---
status: accepted
---

# 外部上下文不能成为 Agent Instruction

Context Item 与 Tool Step 结果始终作为外部数据进入模型上下文，不能写入或提升为 system instructions；只有 Agent Definition 和上层应用的受信控制接口可以提供 Agent Instruction。该结构边界不能保证模型完全不受恶意内容影响，但避免运行时主动把检索内容或工具输出提升为高优先级指令，并为后续 Prompt Injection 测试和策略检查提供清晰边界。
