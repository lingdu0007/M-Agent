---
status: accepted
---

# Eval 作为 Runtime Companion 保留

Eval 由 M-Agent 作为 Runtime Companion 维护，可以启动受控测试 Agent Run 或读取已有 Run Record，评测最终输出、结构化字段、Tool/Context 轨迹、Policy Decision 与恢复行为；Eval Report 独立保存，不参与 Runner 核心循环，也不改变生产 Run Store。默认 evaluator 保持确定性，LLM-as-judge 必须表现为显式、可追踪的评测 Agent Run，从而保留回归测量价值而不把线上 Policy 与离线 Eval 混合。
