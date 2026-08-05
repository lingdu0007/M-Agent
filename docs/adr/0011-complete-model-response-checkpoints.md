---
status: accepted
---

# 只有完整模型响应形成 Checkpoint

流式 Model Step 的输出增量只作为带有 `attempt_id` 的 Run Update 发布，不写成可恢复的步骤结果；只有完整模型响应成功持久化到 Run Store 后，该 Model Step 才完成并形成 Checkpoint。中断后 Runner 以新的 Step Attempt 重新调用模型并通知上层替换先前未完成输出，这可能导致用户看到重新生成，但避免把不同尝试的文本拼接成一个不存在的模型响应。
