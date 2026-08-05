---
status: accepted
---

# 重试由标准失败分类和显式 Retry Policy 驱动

Model、Context Provider 与 Tool 适配器把失败归一化为 `TRANSIENT`、`PERMANENT` 或 `UNCERTAIN`，Runner 只按 Definition Snapshot 中显式、有限的 Retry Policy 执行最大尝试次数、退避和超时；未配置策略时默认不自动重试。只有瞬时且影响类型允许重试的步骤可以自动重试，永久失败不重试，不确定的非幂等副作用进入 `WAITING`，从而避免通过异常文本猜测或产生隐藏成本。
