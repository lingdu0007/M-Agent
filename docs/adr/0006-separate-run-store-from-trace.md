---
status: accepted
---

# Run Store 与 Trace 分离

M-Agent 使用 Run Store 持久化权威的 Run Status、Run Step 和 Checkpoint，恢复机制只读取 Run Store；Trace 仅作为可采样、脱敏、丢弃或导出到外部系统的诊断数据。虽然两者会记录部分重复事实，但分离后观测策略不会影响执行正确性，即使 Trace 完全丢失，Agent Run 仍可依据 Run Store 恢复。
