---
status: accepted
---

# 核心使用 Telemetry Sink 并提供 OpenTelemetry Adapter

Runtime Core 通过轻量 Telemetry Sink 发出关联 `run_id`、`step_id`、`attempt_id`、时间、耗时、状态、标准错误码和可用 usage 的结构化 Trace，默认不包含 Run Payload；官方可选 OpenTelemetry Adapter 将 Run、Step 与 Attempt 映射为 span 并接收上层 trace context，JsonlTracer 作为本地适配器保留。核心不强依赖 OpenTelemetry SDK，也不自建观测平台或 Dashboard，从而兼顾标准集成与轻量嵌入。
