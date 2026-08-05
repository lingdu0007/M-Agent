---
status: accepted
---

# Agent Definition 不可变且显式版本化

每个 Agent Definition 具有稳定 `definition_id` 和不可变 `version`，Agent Run 首次启动时把指令、模型配置、Tool、Context Provider 与运行策略保存为 Definition Snapshot；修改定义会产生新版本，只影响之后的 Run。旧 Run 恢复时必须使用原版本而不能静默切换到最新配置，这增加了版本保留成本，但避免同一次执行在中断前后混用不同 Prompt、模型或能力契约。
