---
status: accepted
---

# 将 M-Agent 定位为 Agent Application Runtime

M-Agent 采用可嵌入 Python 应用的 Agent Application Runtime 定位，负责 Agent 执行生命周期、工具调用、运行状态、策略检查以及 Trace 和 Eval 扩展点。它不发展为一体化 Agent 平台，也不拥有 RAG 内部实现、具体业务逻辑、模型推理服务、分布式调度或容器沙箱；这些能力通过边界接口或上层应用组合，以保持运行时聚焦并避免与独立项目重复。
