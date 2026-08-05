---
status: accepted
---

# 使用确定性 Run Policy 覆盖完整执行生命周期

M-Agent 以 Run Policy 取代只检查工具调用的 Guardrail，并在 `INPUT`、`CONTEXT`、`TOOL_REQUEST`、`TOOL_OUTCOME` 和 `FINAL_OUTPUT` 五类 Policy Gate 返回 `ALLOW`、`REJECT(code)` 或 `REQUIRE_RESOLUTION(code)`。首版 Policy 必须是确定性代码，需要模型判断的审核以后建模为显式 Model Step；这带来现有 Guardrail API 的迁移，但使安全拒绝、结果检查和人工处置都成为可记录、可测试的执行语义。
