---
status: accepted
---

# Eval 作为 Runtime Companion 保留

Eval 由 M-Agent 作为 Runtime Companion 维护，以隔离的 `EXECUTE` 模式启动受控 Agent Run，或以严格只读的 `OBSERVE` 模式评估应用显式选择的已有 Run；两种模式都归一化为不可变 Eval Observation，并通过授权 Observation Projection、版本化 Evidence Adapter 和稳定 Evidence View 交给 Evaluator。Eval Execution、不可变 Eval Report revision 和显式 Baseline 保存在独立 Eval Store，不能修改生产 Run Store、Session Store 或被评估 Run；默认发布门槛只依赖确定性 hard/safety evaluator，多维质量、成本和延迟不压成单一总分。LLM-as-judge 必须表现为专用 Eval Run Store 中独立、版本化、无业务 Tool 和写能力的 Agent Run，只接收最小脱敏 Projection，且永远不能覆盖 required deterministic 或 safety failure；该隔离增加了证据、存储与版本管理成本，却避免评估规则成为生产副作用入口、数据权限旁路或不可复现的模型比较。
