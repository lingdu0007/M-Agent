---
status: accepted
---

# Context Pipeline 以预算关口和显式步骤管理上下文变换

M-Agent 由 Runtime Core 定义并冻结线性 Context Plan、`RUN_INPUT`/`TOOL_OUTCOME`/`MODEL_STEP` Scope、Stage/Frame 恢复事实和业务 Model Step dispatch 前的完整请求硬预算关口；官方 Context Companion 提供 Provider、筛选、排序、去重、机械裁剪和预算选择等透明 Stage，Core 不内置供应商 tokenizer、检索或内容重要性算法。每个 Stage invocation 独立 Checkpoint，同一 invocation 恢复不刷新外部数据；最终候选 Context Frame 先保留受保护证据，只有通过与 Model Adapter 契约指纹一致、精确或保证不低估的 Model Input Sizer 检查后才形成可 dispatch Frame Checkpoint，超预算则以 `CONTEXT_BUDGET_EXCEEDED` 确定性失败而不隐式裁剪、压缩或等待。任何调用模型的 Semantic Compression 都是 `purpose=CONTEXT_COMPRESSION` 的独立、可追踪、可计费 Model Step，使用版本化有损 Compression Contract、保留原始 Item 与派生引用，且不递归触发 Pipeline、业务 Tool 或 Output Repair；该设计增加了公开契约和持久化复杂度，但避免隐藏调用、恢复漂移、预算低估和无法审计的语义损失。
