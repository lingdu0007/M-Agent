---
status: accepted
---

# 使用小型强类型核心与可选 extras

M-Agent 不再以零依赖为目标，基础包使用 Pydantic 承载公开数据契约与验证，异步模型 HTTP、SQLite、受保护 Payload Codec 等能力分别通过 provider、storage 与 security extras 提供；Runtime Core 不引入 Web 框架、数据库服务客户端或 Agent/RAG 框架。Telemetry Core 只拥有标准库 `TelemetrySink` / JSONL 契约；官方 Adapter 的本地 exporter 与 SDK-形状 bridge 只依赖公共 Protocol，不要求 OpenTelemetry SDK。任何实际 SDK-backed exporter 属于 `telemetry` extra 和应用集成边界，绝不成为 Core 依赖或 Collector 声明。该选择增加了依赖管理，但避免继续手写薄弱的 Schema、参数和结构化输出验证，同时维持部署边界。
