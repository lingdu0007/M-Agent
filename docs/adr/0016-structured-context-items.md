---
status: accepted
---

# Context Provider 返回结构化 Context Item

Context Provider 和产生新内容的 Context Stage 不返回失去来源信息的裸文本，而是返回不可变 Context Item：包含 Run 内稳定唯一的 `item_id`、内容、来源、直接输入项引用、稳定变换类型、标准安全/版本字段，以及带 namespace 的有界 JSON 扩展 metadata。排序和筛选只记录顺序或决定，裁剪与语义压缩必须产生引用原始项的新 Item；Core 验证序列化、引用完整性和模型投递 allowlist，但不解释检索分数、相关性或变换算法。该契约增加了适配与存储成本，却为外部 RAG、Eval、引用、敏感数据控制和问题归因保留了共同证据边界。
