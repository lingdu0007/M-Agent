---
status: accepted
---

# 未捕获工具异常不作为模型结果

Tool Step 只有显式 `SUCCESS(result)` 与 `REJECTED(code, message)` 才形成模型可见的 Tool Outcome，其中业务拒绝属于正常完成；未捕获异常是失败的 Step Attempt，由 Runner 按恢复与重试策略处理，内部异常和堆栈只进入受控 Trace。相比把所有异常格式化成字符串交给模型，这一契约要求工具作者区分业务拒绝，但避免模型把基础设施故障当成业务事实，也降低内部信息泄露风险。
