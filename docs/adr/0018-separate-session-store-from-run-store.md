---
status: accepted
---

# 用 Session Store 取代泛化 Memory 抽象

M-Agent 使用 Run Store 保存单次 Agent Run 的执行状态，使用 Session Store 保存多次 Run 共享的对话历史，不再以泛化 `Memory` 同时指代两者；长期记忆、用户画像和语义记忆则通过 Context Provider 或 Tool 由外部能力拥有。现有 `Memory` 与 `SessionMemory` API 将迁移到统一 Session Store 契约，这会带来公开 API 变更，但消除执行恢复、对话连续性和知识检索之间的职责混淆。
