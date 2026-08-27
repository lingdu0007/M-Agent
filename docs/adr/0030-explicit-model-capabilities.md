---
status: accepted
---

# 模型能力显式声明且禁止静默降级

具体 Model Adapter 实例必须以版本化 Model Contract 声明 streaming、tool calling、原生 structured output 与 usage reporting 的类型化模式、可用组合、定量 Limits、字段级 usage 保证和 Sizer 身份，Agent Definition 以 Model Requirements 声明所需能力并冻结完整 Model Binding Set；Definition Registry 在注册时校验兼容性，Runner 不在执行中静默关闭能力、伪造 usage、改变输出语义或切换模型。prompt JSON、Output Repair 等 fallback 只有在 Definition Snapshot 中显式声明时才可执行，Model Fallback 则只允许在 Agent Run 创建前由 Runtime Companion 选择另一完整 Agent Variant；该设计增加了实例级声明和版本管理成本，但使模型替换、能力组合与供应商违约的语义可见且可测试。
