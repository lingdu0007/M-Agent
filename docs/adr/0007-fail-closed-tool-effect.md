---
status: accepted
---

# Tool Effect 默认按 NON_IDEMPOTENT 处理

每个 Tool 通过 `READ_ONLY`、`IDEMPOTENT` 或 `NON_IDEMPOTENT` Tool Effect 声明重试安全性：只读工具可以自动重试，幂等工具使用稳定幂等键后可以自动重试，非幂等工具在结果不确定时转入 `WAITING`。未声明的工具默认视为 `NON_IDEMPOTENT`；这种 fail-closed 默认会降低旧工具的自动恢复便利性，但避免运行时在缺少明确信息时重复执行外部副作用。
