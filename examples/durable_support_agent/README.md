# Durable Support Agent — 旗舰示例与确定性验收（Ticket 11）

M-Agent Durable Run 的**旗舰可执行示例**：把 runtime 已交付的能力组合
成一条生产形态的垂直路径，并给出可重复的确定性验收证据。

## 场景

一个支持 Agent 处理工单：

1. **Context Step**：Runner 在模型之前确定性注入 **ticket 与 policy
   Context Items**（带 `item_id` / `content` / `source` / `metadata`，
   作为数据而非指令，ADR 0014 / 0016 / 0017）；
2. **READ_ONLY order lookup**：查询订单状态（ADR 0007 显式声明，
   可安全重放）；
3. **IDEMPOTENT ticket update**：以 `ticket_id + note` 的稳定 identity
   更新工单；外部 JSONL ledger 在进程间锁下只记录第一次真实更新，重复
   调用返回同一个确定性结果；
4. **NON_IDEMPOTENT notification**：给客户发通知（**外部效果写入独立
   journal**，重复执行可观测）；
5. **崩溃**：通知效果已发生、Tool Step checkpoint 提交**之前**，
   第一进程硬崩溃（`os._exit`，不运行 finally）；
6. **第二进程恢复**：重开同一 SQLite RunStore，通过公开 Runner 恢复
   ——运行时**不重放**非幂等通知，进入 `WAITING`（reason 机器可读的
   `UNCERTAIN_NON_IDEMPOTENT`）；
7. **应用处置**：`CONFIRM_STEP` 提供确认结果（ADR 0008），Run 到达
   `SUCCEEDED`，最终输出确定性的结构化结果。

一键验收还先单独演示 ticket update 的崩溃窗口：外部更新发生、成功
checkpoint 尚未提交时第一进程退出，第二进程通过公开 `resume_run` 自动
重放该 `IDEMPOTENT` Step。原始与恢复 Attempt 都保留在该 RunStore 中，
而外部 ledger 仍只有一次更新。

这条路径覆盖 PRD User Stories 63–66 与 Ticket 11 的全部 Acceptance
criteria：ticket/policy Context Items → READ_ONLY 查询 → IDEMPOTENT
更新 → NON_IDEMPOTENT 通知 → 通知后 checkpoint 前崩溃 → 第二进程
WAITING → CONFIRM_STEP → SUCCEEDED。

## 目录

| 文件 | 职责 |
| --- | --- |
| `support_agent.py` | 共享确定性组件：Definition、Context Provider、模型、三个工具与外部证据文件 |
| `worker.py` | 跨进程执行：idempotent ticket-update replay、notification crash/recovery 与 application confirmation |
| `eval.py` | 确定性 Eval（Runtime Companion）：11 项验收检查，报告独立于 RunStore |
| `run_acceptance.py` | 一键运行编排，退出码反映 acceptance 成败 |
| `README.md` | 本文档 |

## 一键运行

```bash
python examples/durable_support_agent/run_acceptance.py
```

- 默认在临时目录生成全部产物（数据库、journals、日志、证据、报告），
  结束后打印产物目录；可用**空的** `--workdir DIR` 指定固定目录以便
  检查。非空目录会被拒绝，避免旧外部 effect 证据污染新的验收；
- **退出码**：全部验收检查通过为 `0`，场景或验收环节失败为 `1`，无效
  命令行输入（包括非空 `--workdir`）为 `2`；
- 示例默认**离线且可重复**：全部适配器都是 `deterministic=True` 的
  fake，不访问任何网络或供应商，不依赖外部服务。

也可以分步运行（便于观察每一步的原始输出）：

```bash
# IDEMPOTENT ticket update：第一次外部更新后、checkpoint 前崩溃。
python examples/durable_support_agent/worker.py \
    ticket-replay.sqlite ticket-replay-notify.journal ticket-replay.journal \
    ticket-replay-logs ticket-update-and-crash

# 第二进程通过公开 resume_run 自动重放；ticket-replay.journal 仍只有一行。
python examples/durable_support_agent/worker.py \
    ticket-replay.sqlite ticket-replay-notify.journal ticket-replay.journal \
    ticket-replay-logs resume-after-ticket-update <ticket_replay_run_id>

# 第一进程：执行并在通知后、checkpoint 前崩溃（约定退出码 17）
python examples/durable_support_agent/worker.py \
    run.sqlite notify.journal ticket-update.journal logs notify-and-crash

# 第二进程：恢复进入 WAITING，再由应用 CONFIRM_STEP 完成
python examples/durable_support_agent/worker.py \
    run.sqlite notify.journal ticket-update.journal logs \
    resume-and-confirm <run_id>

# 确定性 Eval：读取公开产物 + fake external evidence，写独立报告
python examples/durable_support_agent/eval.py \
    run.sqlite notify.journal ticket-update.journal logs \
    recovery_evidence.json report
```

## 外部证据与 RunStore 分离

NON_IDEMPOTENT 通知的副作用**不**记录在 RunStore 里，而是追加到
独立的 `notify.journal`（每次调用一行）。恢复是否重复通知、通知到底
发生了几次，都由这个 RunStore 之外的证据确定性判定；Eval 的
`notification_once` 检查断言它恰好一行。

`ticket-update.journal` 是不同语义的外部 JSONL ledger。每条记录含
`ticket_id`、`note`、稳定 `idempotency_key` 和确定性 result；写入前以
进程间文件锁扫描同一 key，只有未出现的 key 才执行并 `fsync` 首次外部
更新。因此它的一行代表一次真实 effect，而不是重复 journal 写入后的
最终值。模型请求（`logs/model_request.log`）与 Context Provider 调用
（`logs/provider.log`）是第二组独立证据。

## 确定性 Eval（Runtime Companion，ADR 0029）

`eval.py` 只做两件事：

- 通过**公开只读查询路径**（`Runner.inspect_run`）读取权威 RunStore
  的 Run / Step / Attempt / Checkpoint；
- 读取 **fake external evidence**（journals、logs、恢复证据）。

它不参与 Runner 核心循环、不修改 RunStore；报告写入 `report/` 目录
（`report.json` 机器可读 + `report.txt` 人类可读），位于 RunStore
之外。11 项检查对照 Ticket 11 Acceptance criteria：

1. `context_items_checkpointed` — ticket/policy Context Items 保留
   item_id/content/source/metadata；
2. `context_items_delivered_as_data` — Context Items 的完整
   item_id/content/source/metadata 以数据身份出现在模型每次请求输入中
   （ADR 0017，非 Agent Instruction）；
3. `context_provider_not_refetched` — Provider 只调用一次（恢复复用
   checkpoint，不重新查询外部数据源）；
4. `tool_effects_declared` — order_lookup=READ_ONLY、ticket_update=
   IDEMPOTENT、notify=NON_IDEMPOTENT；
5. `step_trajectory` — Step 顺序 CONTEXT, MODEL, TOOL, MODEL, TOOL,
   MODEL, TOOL, MODEL，每个 Step 均有唯一 Attempt identity，checkpoint
   identity 不复用，且幂等更新 journal 恰一行；
6. `ticket_update_idempotent_recovery` — ticket update effect 后、checkpoint
   前崩溃；第二进程自动重放，原始与恢复 Attempt 都存在，外部 ledger
   仍恰一条；
7. `uncertain_effect_recorded` — 恰一个 UNCERTAIN 失败 Attempt
   （`effect_unconfirmed`），即崩溃发生在通知后、checkpoint 前；
8. `notification_once` — 外部 notify journal 恰好一行；
9. `waiting_and_resolution` — 恢复进入 WAITING（reason
   `UNCERTAIN_NON_IDEMPOTENT`，全部四个 resolution action 合法），
   CONFIRM_STEP 复用同一 step_id 写入确认结果，原始 uncertain Attempt
   与确认 Attempt identity 明确不同；
10. `terminal_succeeded` — Run 终态 SUCCEEDED；
11. `final_structured_result` — 最终结构化结果包含预期 ticket / order
    / ticket_update / notification 字段（普通模型输出，无 Output
    Repair）。

## 关于 at-least-once 的诚实说明

Durable Run 提供 **at-least-once** 恢复（ADR 0003），不宣称
exactly-once。本示例如实展示这一语义：

- 通知的外部效果在 checkpoint 提交前已经发生——运行时**不能**假装
  它没有发生，也**不会**自动重放这个非幂等副作用；
- ticket update 是 effect-safe Step：它可能在崩溃后被运行时重放，但
  外部系统依据稳定 idempotency identity 只接受一次更新；这仍是
  at-least-once 调用语义，不是 runtime 对外部系统的 exactly-once 承诺；
- 它进入 `WAITING`，把判断权交给上层应用：应用依据外部证据选择
  `CONFIRM_STEP`（本示例，确认副作用已发生）、`RETRY_STEP`、
  `FAIL_RUN` 或 `CANCEL_RUN`（ADR 0008）；
- 最终结果里的 `notification` 字段正是应用确认结果，而 `notify.journal`
  的一行证明真实通知只发生过一次——两者共同构成"发生一次 + 应用确认"
  的完整证据，而不是伪造 exactly-once 的假象。
