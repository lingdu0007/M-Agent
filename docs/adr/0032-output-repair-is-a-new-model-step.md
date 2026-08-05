---
status: accepted
---

# Output Repair 创建新的 Model Step

不符合 Output Contract 的模型响应仍作为完整 Model Step 保存；若契约允许修复，Runner 使用结构化验证错误创建新的 Output Repair Model Step，并受 Definition Snapshot 中的次数上限约束。修复耗尽后 Agent Run 以 `OUTPUT_VALIDATION_FAILED` 进入 `FAILED`，无效输出留在 Run Store 供诊断但不写入 Session Turn，从而保留真实推理轨迹而不把新提示调用伪装成原步骤重试。
