---
status: accepted
---

# Agent Run 采用 at-least-once 恢复语义

M-Agent 对可恢复的 Run Step 提供 at-least-once 语义，不宣称跨外部系统的 exactly-once；运行时保存步骤边界及结果，并可能在无法确认完成状态时重放步骤。Side-effecting Tool 必须声明幂等能力或恢复策略，例如携带幂等键、禁止自动重试或转入人工确认，以避免框架对外部副作用作出无法兑现的保证。
