---
status: accepted
---

# Session 只保存成功 Run 的最终对话轮次

Agent Run 只有进入 `SUCCEEDED` 后才向 Session Store 提交 Session Turn，默认内容仅包含用户输入、最终输出、`run_id` 和必要时间元数据；Context Item、模型中间响应、工具调用与结果仍归 Run Store，失败、取消或等待中的 Run 不写入 Session。该选择减少后续上下文中的冗余和敏感数据传播，但要求上层应用把需要长期复用的事实显式保存并通过 Context Provider 再次提供。
