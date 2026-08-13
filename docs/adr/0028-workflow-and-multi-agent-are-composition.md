---
status: accepted
---

# Workflow 与 Multi-Agent 不属于核心运行时

M-Agent 核心只拥有 Agent Definition、Agent Run、Runner 及 Model、Tool、Context Provider、Run Policy 和 Store 等扩展契约；Workflow 是上层应用对多个步骤或 Agent Run 的编排，Multi-Agent 是其中一种组合模式。现有 `Workflow` 与 `MultiAgentTeam` 将迁移到 `contrib` 或 examples，这减少了核心功能名，但避免把顺序调用包装成独立执行引擎，并确保每个参与者仍产生独立、可恢复和可观测的 Agent Run。
