---
status: accepted
---

# Agent Run 采用协作式取消

Cancellation Request 使 Runner 停止启动新的 Run Step，并尽力中断当前调用，但不承诺回滚外部副作用；只有确认能够安全停止时 Run Status 才转为 `CANCELLED`。若 `NON_IDEMPOTENT` Tool Step 正在执行且结果不确定，Agent Run 必须进入 `WAITING` 并由上层提交 Run Resolution，而不能用 `CANCELLED` 掩盖可能已经发生的外部操作。
