---
status: accepted
---

# 核心运行时不提供 RAG 专用抽象

M-Agent 只提供两种通用外部知识接入方式：上层通过 Context Provider 在 Model Step 前确定性注入上下文，或把检索暴露为 Tool 交给模型选择；核心运行时不定义 Retriever，也不拥有 ingestion、chunking、embedding、索引、rerank 等 RAG 内部能力。现有关键词索引将降为示例或测试适配器，这减少了开箱即用的 RAG 功能，但使独立 RAG 项目可以通过稳定边界组合，而不在两个仓库重复实现检索系统。
