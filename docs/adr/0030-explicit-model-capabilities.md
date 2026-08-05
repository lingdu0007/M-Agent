---
status: accepted
---

# 模型能力显式声明且禁止静默降级

Model Adapter 必须声明 streaming、tool calling、原生 structured output 与 usage reporting 等 Model Capabilities，Agent Definition 声明所需能力，Definition Registry 在注册时校验兼容性；Runner 不在执行中静默关闭能力、伪造 usage 或改变输出语义。只有 Definition Snapshot 明确允许的 fallback 才可使用，这增加了适配器声明成本，但使模型替换的语义差异可见且可测试。
