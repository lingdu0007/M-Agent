---
status: accepted
---

# 分别以模型调用和单个工具调用划分 Run Step

M-Agent 将一次模型调用记录为一个 Model Step，并将模型响应中的每个工具调用分别记录为独立 Tool Step；Guardrail 检查、checkpoint 写入和 Trace 记录是步骤周围的生命周期事件，不是 Run Step。相比把一轮模型响应及全部工具调用作为整体，这种粒度增加了状态记录复杂度，但允许运行时单独恢复、重试或审计每个工具调用，避免重复执行同批次中已经确认完成的工具。
