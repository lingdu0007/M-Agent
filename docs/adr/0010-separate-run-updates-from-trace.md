---
status: accepted
---

# Run Update 与 Trace 分离

Runner 通过稳定的异步 Run Update 接口向上层应用通知 Run Status、Run Step、模型输出增量和 `WAITING` 等进展，具体 SSE、WebSocket 或终端传输由上层决定；Trace 保持可采样、脱敏和演进的内部诊断接口。Run Update 允许实时丢失且不承担恢复职责，应用重连后读取 Run Store，因此增加一种事件契约的成本换来了应用协议、诊断数据和权威状态之间的清晰边界。
