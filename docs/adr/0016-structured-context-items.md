---
status: accepted
---

# Context Provider 返回结构化 Context Item

Context Provider 不返回失去来源信息的裸文本，而是返回包含 Run 内稳定 `item_id`、`content`、`source` 和提供方 `metadata` 的 Context Item；Runner 保留项目顺序及溯源信息，但不解释检索分数、不执行 rerank，也不决定最终引用格式。该契约增加了适配成本，却为外部 RAG、Eval、引用与问题归因保留了共同的证据边界。
