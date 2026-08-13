---
status: accepted
---

# Runner 采用 async-first 嵌入式模型

M-Agent 以异步 Runner 作为创建、推进、恢复、取消 Agent Run 和提交 Run Resolution 的规范执行接口，Model 与 Tool 的规范调用接口同样异步；同步 `run()` 只作为本地脚本和简单示例的便捷包装。框架不内置后台 worker、消息队列或独立服务，这会把进程部署和任务调度留给上层应用，但使运行时能够支持流式输出、并发与取消而不扩张成 Agent Infra 平台。
