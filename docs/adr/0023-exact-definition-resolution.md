---
status: accepted
---

# 恢复时精确解析 Agent Definition 实现

Run Store 只保存 Definition Snapshot 和能力标识，不序列化 Python callable；上层应用通过 Definition Registry 注册并按 `definition_id + version` 精确提供可执行实现。原版本不可用时 Agent Run 进入原因明确的 `WAITING`，等待应用重新注册旧版本或显式失败、取消，而不能回退到最新版本；这要求应用保留仍有可恢复 Run 的旧代码，但避免不安全代码反序列化和静默行为漂移。
