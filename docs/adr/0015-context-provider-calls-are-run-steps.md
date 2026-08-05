---
status: accepted
---

# Context Provider 调用形成 Context Step

每次 Context Provider 调用形成独立、只读的 Context Step，其输入和完整输出写入 Run Store 并形成 Checkpoint；恢复同一 Agent Run 时复用已完成结果，而不是静默重新获取外部上下文。该选择增加了上下文存储量，但避免恢复后的 Model Step 因检索或环境变化获得不同证据，从而提高单次执行的可复现性和可审计性。
