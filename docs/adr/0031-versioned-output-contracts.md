---
status: accepted
---

# Output Contract 属于版本化 Agent Definition

结构化输出通过 Agent Definition 中可选的 Output Contract 声明，契约包含 Schema、验证规则及允许的 fallback，并随 Definition Snapshot 固定；调用方不能为单次 Run 临时替换任意 Schema，契约变化需要新的 Agent Definition 版本。该选择降低了通用 Agent 动态返回多种结构的便利性，但使模型能力校验、恢复执行和 Eval 面对稳定且可追踪的输出语义。
