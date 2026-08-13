---
status: accepted
---

# 不确定副作用只能由上层应用处置

当 `NON_IDEMPOTENT` Tool Step 的外部结果无法确认时，Agent Run 转入 `WAITING`，模型无权自行重试、假定成功或跳过该步骤。嵌入 M-Agent 的上层应用必须显式提交 Run Resolution：`RETRY_STEP`、`CONFIRM_STEP(result)`、`FAIL_RUN(reason)` 或 `CANCEL_RUN(reason)`；这一控制边界需要上层集成审批或运维入口，但能防止模型在事实不明时制造重复副作用或不一致上下文。
