---
status: accepted
---

# 本地文件能力作为 Local Content Adapter 提供

Runtime Core 只定义 Tool 及其影响、结果和执行策略，不直接拥有文件系统权限；现有 `list_files`、`read_text_file` 与 `search_text` 迁移为只读 Local Content Adapter Runtime Companion，并继续受 Project Root 与 Project Content 约束。首版不提供写文件、Shell 或任意代码执行工具，这保留了可展示的真实适配器，同时避免通用运行时暗含高风险本地执行能力。
