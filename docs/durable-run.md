# Durable Run — Ticket 01/02/03/04/05/06/07/08/10 运行时边界、恢复、并发控制、实时更新与协作取消

本文档面向 Runtime Integrator，说明 M-Agent 新公共包 `m_agent` 中
Durable Run 垂直切片的职责边界、恢复语义、并发控制与可观测契约。
领域词汇以根目录 `CONTEXT.md` 为准。

- Ticket 01：first versioned Model Run（公开 Runner tracer bullet）；
- Ticket 02：从 SQLite 崩溃恢复 Model Run（跨进程恢复、Metadata/
  Payload 分离、Payload Codec、DEFINITION_UNAVAILABLE）；
- Ticket 03：Run Lease + 乐观版本控制，同一 Run 只允许一个有效推进者；
- Ticket 04：Context Provider 注入与 checkpoint 复用（Context Step、
  Context Item 溯源、外部数据变化防护、指令边界）。
- Ticket 05：模型请求的单个工具调用形成顺序 Tool Step，dispatch 前持久化
  Step / Attempt 身份，SUCCESS / REJECTED outcome checkpoint 后再继续。
- Ticket 06：结构化失败分类与冻结的有界 Retry Policy；恢复从同一
  Step 的已持久化 Attempt 计算预算，不以新进程的循环计数或当前 Tool
  声明改变重试、WAITING 决策。
- Ticket 07：不确定的非幂等副作用只能由应用通过显式 resolution
  处置；真实进程崩溃后的通知绝不被恢复过程自动重放。
- Ticket 08：Runner 通过非权威 Run Update 暴露流式 Model 进展，并在
  不伪造外部调用撤销的前提下，以公开 `cancel_run` 协作停止后续 Step。

## 职责边界

**Runner 只执行调用方交给它的 Run。**

- `Runner.start_run(run_id)` 从 CREATED 启动；`Runner.resume_run(run_id)`
  从持久化状态恢复一个非终态 Run；
- 推进前 Runner 从 RunStore 排他获取 Run Lease（ADR 0013）；租约
  过期后其他 Runner 才能接管，接管由上层应用显式发起；
- Runner 不承担 worker、queue、scheduler 或后台任务调度职责；
- Runner 不会自动扫描 RunStore、不会排队、不会后台轮询或自动
  takeover；进程部署、任务调度、并发拓扑完全由上层应用控制
  （ADR 0009）；
- Tool Step 只按模型响应中的单个调用划分；同一 Run 内严格顺序执行，
  上一个 Tool Outcome checkpoint 确认后才会 dispatch 下一个调用；
  不实现并行工具调度。
- `Runner.resolve_run` 是上层应用提交不确定副作用 resolution 的唯一
  控制入口；Runner 不为模型、Tool Outcome 或 Context Item 提供该命令
  通道（ADR 0008）。Context Step 自 Ticket 04 起已实现。
- `Runner.cancel_run(run_id, expected_version=None)` 是 Cancellation Request
  的公开控制入口；`Runner.subscribe_run(run_id)` 是 Runtime Integrator
  订阅实时 Run Update 的唯一入口。两者不引入后台 worker、消息队列或
  状态回放服务。

**Run Store 是权威事实来源（ADR 0006）。**

- 恢复与检查只读取 Run Store；
- `InMemoryRunStore` 面向确定性测试与本地实验；
- `SQLiteRunStore` 是持久化参考实现：单文件数据库，进程重启后重开
  同一文件即可恢复；写入在返回前 commit，已确认的 checkpoint 落盘
  后进程崩溃不会丢失。

## 状态词汇与终态

Run 状态只能是 `CREATED`、`RUNNING`、`WAITING`、`SUCCEEDED`、
`REJECTED`、`FAILED`、`CANCELLED`（CONTEXT.md）。其中
`SUCCEEDED`、`REJECTED`、`FAILED`、`CANCELLED` 是终态。

- 转换校验集中在一张合法转换表（`m_agent/_status.py`）；
- 非法生命周期命令与基于过期版本号的更新显式失败，且不改动权威
  Run 记录（`IllegalRunTransitionError` / `StaleRunVersionError`）。

已开放的转换：`CREATED -> RUNNING | CANCELLED`，
`RUNNING -> SUCCEEDED | FAILED | WAITING | CANCELLED`，
`WAITING -> RUNNING | FAILED | CANCELLED`。`WAITING -> RUNNING` 只由
精确 Definition 恢复或应用 resolution 驱动；`REJECTED` 的转换由后续
Ticket 扩展。

## Run Update 与协作式取消

Ticket 08（ADR 0010 / 0011 / 0012）为 Runtime Integrator 提供实时进度和
协作式控制，但 RunStore 仍是唯一权威事实来源。

- **订阅与重连**：应用只通过 `Runner.subscribe_run(run_id)` 异步订阅
  live-only 的 `RunUpdate`。订阅者断开、停止消费或自身处理异常只移除
  订阅，不改变 Run 执行或 RunStore；重新订阅不承诺历史 replay，应用应
  通过 `Runner.get_run` / `Runner.inspect_run` 重新读取权威状态。
- **流式 Model 进展**：`MODEL_DELTA` 包含 `run_id`、Model `step_id` 和
  `attempt_id`；同一 Attempt 的 identity 稳定。部分 delta 绝不写入
  checkpoint。流式 Attempt 失败后 retry 会创建新的 `attempt_id`，消费者
  可丢弃旧 Attempt 的部分输出；只有完整 `ModelResponse` 成功持久化为
  checkpoint 后才发布 `STEP_COMPLETED`，且 checkpoint 先于 Tool dispatch
  或最终状态提交。
- **取消请求**：CREATED Run 可立即安全进入 CANCELLED，零 Provider / Model /
  Tool 调用。正在由同一 Runner 推进的 RUNNING Run 只登记 Cancellation
  Request；推进者在每个新 Context / Model / Tool dispatch 前、流式 delta
  之间，以及已 dispatch 的 Model / Tool 调用返回并保存可确认 Step 证据
  后检查它。取消阻止任何后续 Step 与 `SUCCEEDED` 提交，但不声称已经
  dispatch 的调用被强制中断、回滚或撤销。
- **不确定副作用与并发**：取消期间 NON_IDEMPOTENT Tool 的结果仍不确定时，
  Run 必须进入 `WAITING(UNCERTAIN_NON_IDEMPOTENT)`，只能由应用按 Ticket
  07 Resolution 处置，不能用 CANCELLED 掩盖。过期 `expected_version`、
  终态后的取消与重复取消显式失败且不改动权威记录；不掌握活跃推进循环的
  Runner 也不能仅凭租约过期抢写 CANCELLED（ADR 0013）。

## 恢复语义：at-least-once

Durable Run 提供 **at-least-once** 恢复（ADR 0003），**不宣称
exactly-once**：

- 已确认持久化的 Model Step Checkpoint 在恢复时被复用，不重复调用
  模型——这是崩溃后 checkpoint 已落盘、终态未写入场景的行为；
- 模型调用完成但结果尚未 checkpoint 的崩溃（或任何未确认完成的
  Step）会在恢复时重新执行，因此模型调用可能发生不止一次；
- 外部副作用的安全由 Tool Effect 声明、失败分类、Retry Policy 与
  显式应用处置表达（后续 Ticket）；运行时不为外部系统承诺
  exactly-once。

跨进程恢复测试（`tests/test_m_agent_resume.py`）启动真实子进程，
在确定性的 :class:`CrashPoint` 用 `os._exit` 硬退出（不运行
finally/atexit、不留存 Python 对象），再由第二进程重开同一 SQLite
数据库、通过同一公开 Runner API 恢复。崩溃进程遗留的 Run Lease 在
TTL 内仍有效，恢复 Runner 必须先等租约过期才能接管（测试用
:class:`FakeClock` 确定性推进时钟越过过期点，而不是真实 sleep）；
接管后从最新安全 checkpoint 继续，不重复调用模型。

## Run Lease 与并发控制

ADR 0013 / PRD User Stories 21-23, 44：同一 Run 只允许一个有效推进者，
不同 Run 可并发推进，运行时只提供协调原语，不做调度。

- **租约原语**：
  `RunStore.acquire_lease(run_id, owner, ttl, expected_version=...)` 排他授予
  单个 Runner 带期限的推进权；owner / 期限作为持久状态保存在
  RunStore（SQLite 的 `runs.lease_owner / lease_expires_at` 列），
  可通过 `get_lease` 与 Run 记录验证。获取、释放与续约都校验
  `expected_version`，但租约变化本身不递增 Run progress 版本号。
- **两个关键时点**：Runner 在每个新的 Context / Model / Tool Attempt
  dispatch 前重新校验 `expected_version + owner + expires_at`，并在同步
  `STEP_STARTED` Update / Telemetry 回调和请求构造完成后、真正调用
  Provider / Model / Tool 的最后边界再次校验，不以旧的进程内 Lease
  对象推断当前所有权；每次权威提交（Lease / Step /
  Attempt / Checkpoint / 状态转换）也在 Store 侧原子执行相同的版本
  与租约条件。旧 owner 的迟到提交得到显式冲突
  （`LeaseNotHeldError` / `StaleRunVersionError`），绝不覆盖新状态。
- **过期与接管**：租约过期后其他 Runner 才能接管（`acquire_lease`
  允许无租约 / 已过期 / 原 owner 续约三种情形）；接管是上层应用
  显式调用 `resume_run` 的行为，运行时没有后台扫描、自动 takeover、
  queue 或 scheduler。
- **释放**：正常到达终态后 Runner 显式释放租约；异常中断（含模拟
  崩溃）时租约保留至过期，供后续 Runner 接管。`release_lease` 同时
  校验 expected version 与 owner，不会误释放他人租约。
- **可注入时钟**：过期与接管测试使用 :class:`FakeClock` 确定性推进
  （`advance`），禁止依赖真实 sleep 的时间敏感断言；生产默认使用
  系统时钟（`SystemClock`）。
- **语义边界**：租约有效期覆盖单次推进（模型调用 + 提交）。模型
  调用时长超过 TTL 时租约会过期、后续提交被拒（失去租约后停止
  推进），上层应用需根据模型延迟选择足够大的 `lease_ttl`，或在该
  Run 过期后重新接管。TTL 过期本身不能证明旧 owner 已无 in-flight
  外部调用；因此不掌握本地推进循环的 Runner 不能抢写 `CANCELLED`。

## Definition 与精确解析

- `AgentDefinition` 不可变、带版本（ADR 0022），注册时校验所需
  Model Capabilities 已被所选 Model Adapter 声明（ADR 0030），
  无静默降级；
- Run 启动时冻结 `DefinitionSnapshot`（ADR 0022 / 0023），Run Store
  不序列化 Python callable；
- 恢复时按精确 `definition_id + version` 解析（ADR 0023）。原版本
  缺失时 Run 进入 `WAITING`，`waiting_reason` 为机器可读的
  `DEFINITION_UNAVAILABLE`；**绝不自动回退到最新版本**。应用重新
  注册精确旧版本前，公开 `resume_run` 保持 WAITING、不改动既有
  checkpoint；旧版本恢复注册后，应用再次调用 `resume_run`，Runner 才
  在 lease/version 保护下重走安全恢复路径。该 reason 没有等待中的
  Tool Step，只允许 `FAIL_RUN` 或 `CANCEL_RUN`，不开放 `RETRY_STEP` /
  `CONFIRM_STEP`。

## Run Metadata 与 Run Payload

- 可查询的 Run Metadata 只保留定义标识与版本、状态、Run version、
  `WAITING` reason、时间、Step 类型、失败 classification 与稳定
  `error_code`。`DefinitionSnapshot.instructions`、模型内容、输入/输出、
  Checkpoint 内容和受保护的 Attempt 诊断详情属于 Run Payload，不能写入
  metadata JSON（ADR 0033）；
- Payload 只能经上层应用为 RunStore 显式配置的 :class:`PayloadCodec`
  读写，任何存取路径都不绕过 Codec；
- `PlaintextPayloadCodec` 是显式的开发/测试 Codec，编码带
  `m-agent-plaintext:` 标记；**它不是生产默认**，也不宣称提供任何
  保密性。生产集成必须选择符合自身安全要求的受保护 Codec
  （security extras，后续 Ticket）；
- provider 凭证（API Key、访问令牌）只存在于 Adapter 外部配置，
  不进入 Definition Snapshot、Run Payload、Checkpoint、Attempt 诊断或
  存储 Metadata（`tests/test_m_agent_credentials.py` 对 InMemory 与
  SQLite 全程验证）。凭证隔离是结构性保证：运行时从不接收或转发
  凭证；Adapter 不得把密钥嵌入请求内容、响应或异常。
- 失败 Attempt 的 classification 与 `error_code` 可用于查询和恢复决策；
  `error_code` 是 Adapter 提供的非可信输入，只有运行时显式 allowlist 中的
  稳定身份会保留，未知值一律写成 `unsafe_error_code`。相同的净化值同时
  用于 Attempt metadata 与 Telemetry，避免两个观测面一边掩码、一边泄漏；
  Attempt 检查返回的诊断同样是固定安全摘要，不以 `str(exc)`、Adapter
  message、provider 文本或其他原始异常内容写入 metadata 或 payload。未分类
  异常只保留异常类型的安全诊断，避免不合规 Adapter 的原始错误把凭证持久化。

## 模型适配器

- `ModelAdapter` 是 live provider 适配器的基类（`deterministic=False`）；
- `DeterministicModelAdapter` 是确定性 fake（`deterministic=True`），
  仅用于测试、演示与离线示例——任何确定性测试都不会被误认为
  供应商兼容性验证；
- **Live provider Adapters**（Ticket 10，ADR 0030 / 0038）：OpenAI
  兼容 Chat Completions 与 Responses 风格 API 的 live Adapter 位于
  可选 `m_agent.provider` 子包（`provider` extra，依赖 `httpx`）。
  两者都如实声明 streaming / tool calling / native structured
  output / usage reporting 能力，并拥有凭证门控契约测试（经公共
  Runner seam 验证真实 provider 行为；默认离线 CI 排除；pytest 还须
  同时显式选择 `-m live` 与 `M_AGENT_RUN_LIVE_TESTS=1`）。详见
  [`docs/live-model-adapter-contracts.md`](live-model-adapter-contracts.md)。

## Context Provider 与上下文注入

Ticket 04（ADR 0014 / 0015 / 0016 / 0017）：

- Definition 可声明一个应用选择的 `context_provider`（ADR 0014）。
  Runner 在依赖它的 Model Step **之前**确定性执行它（ADR 0015）：
  每次调用在 dispatch 前先持久化独立、只读的 CONTEXT Step 与 RUNNING
  Step Attempt identity，并在最后 dispatch 边界校验 Run Lease / version；
  完整输出（序列化的 Context Items）写入 Run Store 并形成 Checkpoint，
  然后才执行 Model Step；
- Provider 返回带 `item_id` / `content` / `source` / `metadata` 的
  `ContextItem`（ADR 0016）；运行时保留顺序与溯源，但不解释检索
  分数、不执行 rerank、不决定引用格式；
- **恢复复用**：Context Step checkpoint 已确认、Model Step 未执行时
  崩溃，恢复直接复用 checkpoint 中的 Context Items，**不重新查询外部
  数据源**——外部数据即使变化，也不会重写 Run 的既有上下文；
  Context checkpoint 落盘前崩溃则重新执行（at-least-once）；
- **指令边界**（ADR 0017）：Context Items 作为不可信数据经
  `ModelRequest.context_items` 交付给模型，永远不能写入或替换受信的
  `instructions`（Agent Instruction）。Chat Completions 映射只有一条
  system 消息，且其内容精确为冻结的 Definition instructions；每个
  Context Item 的完整 `item_id` / `content` / `source` / `metadata` 作为
  JSON user data 消息。Responses 映射的 `instructions` 同样精确等于冻结
  Definition instructions，Context Item 只位于 `input` data；
- **安全保证范围**：上述是数据通道与权限层级隔离，防止运行时把外部
  内容提升为 Agent Instruction，同时保留 provenance 供检查。它**不**
  保证模型不会遵循恶意 Context Item 中的 prompt injection；集成方仍应
  根据其风险模型采取输出校验、策略与人工处置；
- **失败语义**：Provider 异常形成可检查的 FAILED Step Attempt（稳定
  classification / error_code 加经 Codec 保护的诊断），Run 到达 FAILED，
  绝不把异常压平成模型可见的上下文字符串，也不会继续执行 Model Step；
- **检索集成边界**：确定性检索（应用选择）作为 Context Provider，
  模型选择的检索未来走普通 Tool 契约，运行时不为 RAG 提供特权
  子系统（ADR 0014）。最小示例见
  `docs/context-provider-vs-tool.md`。

## 顺序 Tool Step

Ticket 05（ADR 0004 / 0007 / 0024）把模型请求的每个工具调用作为独立的
Tool Step：

- dispatch 前，Runner 使用稳定的 `step_id` 与 `attempt_id` 记录
  `RUNNING` Tool Step / Step Attempt；因此工具已被调用、但 outcome
  checkpoint 尚未提交的崩溃仍有可查询的权威身份；
- 工具只可返回结构化 `ToolOutcome.SUCCESS` 或 `ToolOutcome.REJECTED`；
  两者都作为不可信数据持久化并传给下一次 Model Step，不能修改
  Agent Instruction；
- 返回 outcome 后，同一身份更新为 `SUCCEEDED`，然后写入 Tool
  checkpoint；未捕获异常则将该 Attempt 更新为 `FAILED`，不伪造成模型
  可见 outcome；
- 同一模型响应中的调用按声明顺序串行执行；每一个 Tool checkpoint
  都先于下一次 Tool dispatch 或 Model Step；
- 工具 effect 只允许 `READ_ONLY`、`IDEMPOTENT`、`NON_IDEMPOTENT`；
  未声明时按 `NON_IDEMPOTENT` fail-closed。

## 有界 Retry Policy

Ticket 06（ADR 0007 / 0025）为 Model 与 Tool Adapter 的失败提供结构化
`TRANSIENT`、`PERMANENT`、`UNCERTAIN` 分类和稳定 `error_code`；原始异常
文本不进入可查询 metadata。失败 Attempt 保留分类、错误码、时间和
`attempt_id`，每次重试只创建新的 Attempt，绝不覆盖已有证据。

- 只有 Definition Snapshot 中显式、冻结的 Retry Policy 才允许自动重试，
  `max_attempts` 是同一权威 Step 的全部已持久化 Attempt 上限；无策略总计
  只能调用一次。SQLite 重开、进程重启或取消不能重置该预算。
- 只有 `TRANSIENT` 进入自动重试判断；`PERMANENT`、`UNCERTAIN` 和未分类
  失败均不作为普通 retry。到达上限后 Runner 直接形成确定性的 `FAILED`，
  不再调用 adapter 或 Tool。
- 同一未完成 Model / Tool Step 恢复时复用原 `step_id`，合法 retry 使用新
  `attempt_id`。已 dispatch 但未 checkpoint 的 READ_ONLY/IDEMPOTENT Tool
  保留 ADR 0003 的 at-least-once 恢复语义；这不是把 Adapter 的
  `UNCERTAIN` 分类当作 retry。
- Tool retry、恢复和 uncertain-effect 决策只读取 Snapshot 的
  `tool_declarations`。原始 Snapshot 是 `NON_IDEMPOTENT` 时，即使恢复
  进程重注册为 `READ_ONLY`，已发出的未确认 Step 仍进入
  `WAITING(UNCERTAIN_NON_IDEMPOTENT)`，不自动重放。快照的 Tool 声明
  缺失或歧义时 fail closed，绝不从当前 callable 猜测安全性。

## 不确定副作用的应用处置

Ticket 07（ADR 0007 / 0008）处理非幂等 Tool 已产生外部效果、但成功
checkpoint 尚未提交时的安全边界。测试中的通知把效果写入独立 journal，
第一进程在 `BEFORE_TOOL_CHECKPOINT` 硬退出；第二进程只打开同一 SQLite、
外部 journal 和精确重注册的 Definition。

- 恢复读取冻结的 `ToolDeclaration.effect`，不读取当前 Tool callable 的
  effect。未确认的冻结 `NON_IDEMPOTENT` Tool Step 不执行 Tool：原始
  Step / Attempt 被记录为 `UNCERTAIN`，Run 进入
  `WAITING(UNCERTAIN_NON_IDEMPOTENT)`；`waiting_reason`、
  `waiting_step_id` 和 `allowed_resolutions(run)` 都是机器可读的公开
  检查结果。
- 应用只能经 `Runner.resolve_run(run_id, RunResolution, expected_version=...)`
  提交 `RETRY_STEP`、`CONFIRM_STEP(result)`、`FAIL_RUN(reason)` 或
  `CANCEL_RUN(reason)`。`CONFIRM_STEP` 不调用 Tool，而是以应用提供的
  结构化结果完成原 Tool Step / Checkpoint；`RETRY_STEP` 才创建新的
  `attempt_id` 并重执行。失败与取消直接到相应终态，均不执行 Tool。
- resolution 需要 WAITING 状态、有效 lease 和当前 `expected_version`；
  命令可携带观察到的 `waiting_step_id`，提供时必须匹配权威目标 Step。
  stale、duplicate、错误 action / Step、终态命令都会显式失败，且不改动
  Run、Step、Attempt、Checkpoint 或外部效果。ModelResponse、ToolOutcome
  与 Context Item 契约均没有 resolution 字段，不能冒充应用命令。
- 普通 `resume_run` 对 `UNCERTAIN_NON_IDEMPOTENT` 一律保持 WAITING；只有
  上述四种显式应用 resolution 能改变它。

## 测试入口

- 主行为测试只通过公开异步 Runner 的注册、创建、启动、恢复与查询
  路径驱动（`tests/test_m_agent_run.py`、
  `tests/test_m_agent_resume.py`）；
- InMemoryRunStore 与 SQLiteRunStore 共享同一份行为契约测试
  （`tests/store_contract.py` 的 `RunStoreContractMixin`，分别见
  `tests/test_m_agent_store.py` 与 `tests/test_m_agent_sqlite_store.py`），
  含租约获取 / 冲突 / 过期接管 / 迟到提交 / 释放契约；
- 并发租约测试（`tests/test_m_agent_lease.py`）用 SQLite 双连接 +
  可控 gate 模拟两个 Runner 争抢同一 Run，覆盖：单有效 owner、过期
  takeover、旧 owner 迟到提交被拒、过期后 Model / Tool 零新调用、
  telemetry 回调导致的过期 dispatch、cancel race、checkpoint 复用、不同
  Run 并发（无全局锁），断言最终
  Run / Step 记录与共享模型/工具调用计数；
- 跨进程崩溃/恢复与 DEFINITION_UNAVAILABLE 见
  `tests/test_m_agent_resume.py`（子进程：`tests/fixtures/crash_worker.py`）；
- Context Provider 行为、指令边界、Provider 失败与 checkpoint 复用见
  `tests/test_m_agent_context.py`；跨进程 Context 恢复（外部数据变化
  防护、provider 调用计数）见 `tests/test_m_agent_resume.py` 的
  `ContextCrossProcessResumeTests`；checkpoint 的 step_type 持久化
  契约见 `tests/store_contract.py`。Chat Completions / Responses 的真实
  adapter request 构造、wire role 与 provenance 边界由不触网的
  `tests/test_m_agent_context_adapter_boundary.py` 验证；
- 凭证隔离见 `tests/test_m_agent_credentials.py`。
- 顺序 Tool Step、SUCCESS / REJECTED outcome、异常失败 Attempt、effect
  默认值与指令边界见 `tests/test_m_agent_tool_run.py`；工具 dispatch 后、
  outcome checkpoint 前崩溃保留原始 Step / Attempt 身份并可恢复检查见
  `tests/test_m_agent_tool_recovery.py`。
- 失败分类、有界预算、冻结 Retry Policy/Tool Effect 以及 SQLite 重开后的
  Model / Tool 恢复见 `tests/test_m_agent_retry.py`。
- 不确定通知的真实子进程崩溃、外部 journal、四种 resolution、错误
  target Step / stale version 不变性见 `tests/test_m_agent_resolution.py`
  （子进程：`tests/fixtures/notification_worker.py`）；精确旧 Definition
  重新注册后的 checkpoint 复用见 `tests/test_m_agent_resume.py`。
- Run Update、完整流式 checkpoint、partial retry identity、订阅者丢失、
  reconnect reconciliation、协作取消、非流式 in-flight 完整/失败响应与
  Tool-request 取消见 `tests/test_m_agent_stream_cancel.py`；测试只通过
  Runner、RunStore 和 inspection/update 公共 seam 断言权威 Step / Attempt /
  Checkpoint、status、updates 与外部调用计数。

## Ticket 11：Durable Support Agent 旗舰示例与确定性 Eval

旗舰示例把前序 Ticket 的能力组合成一条可执行的验收路径（PRD
User Stories 63–66）：ticket/policy Context Items → READ_ONLY
order lookup → IDEMPOTENT ticket update → NON_IDEMPOTENT
notification → 通知后 checkpoint 前崩溃 → 第二进程恢复 WAITING →
应用 CONFIRM_STEP → SUCCEEDED。它还单独执行 ticket update 的崩溃窗口：
外部效果发生后、Tool checkpoint 前退出；第二进程经公开 `resume_run`
自动重放该 effect-safe Step。

- **位置**：`examples/durable_support_agent/`。`support_agent.py`
  组装确定性 Definition（三个工具分别声明 READ_ONLY / IDEMPOTENT /
  NON_IDEMPOTENT，ADR 0007），并冻结 `RetryPolicy(max_attempts=2)`；
  `worker.py` 只经 `create_run`、`start_run`、`resume_run` 与
  `resolve_run` 演示两类崩溃恢复；`eval.py` 是确定性 Eval；
  `run_acceptance.py` 是一键编排。
- **外部证据与 RunStore 分离**：通知追加到独立 journal；ticket update
  写入含稳定 ticket/note identity 的 JSONL ledger。ledger 用进程间锁与
  `fsync` 让首次 effect 可观察，恢复重放同一 identity 不会追加第二条。
  模型请求与 Provider 调用各写日志；Eval 据此断言通知一次、ticket update
  一次，且 Provider 恢复时不重新查询。
- **确定性 Eval（ADR 0029，Runtime Companion）**：只读公开产物
  （`Runner.inspect_run`）与 fake external evidence，报告写入
  RunStore 之外的 `report/` 目录（`report.json` / `report.txt`），
  11 项检查覆盖完整 context provenance、每个 Step 的唯一 Attempt identity、
  ticket update 的原始/恢复 Attempt 与一次 effect、notification 的原始
  uncertain / CONFIRM_STEP Attempt、一次通知、WAITING 转换、resolution、
  终态与最终结构化结果；退出码反映 acceptance 成败。
- **一键运行**：`python examples/durable_support_agent/run_acceptance.py`
  ——默认离线、可重复，全部检查通过退出码 0，场景或验收失败为 1；显式
  `--workdir` 必须为空，避免复用旧 external evidence（无效命令行输入为
  2）。
- **测试入口**：`tests/test_durable_support_agent_example.py` 与
  `tests/test_durable_support_agent_idempotency.py` 以真实子进程、SQLite、
  public inspection 和外部 ledger 覆盖正向路径及 duplicate notification、
  duplicate ticket update、缺失 Run、错误 Step trajectory、错误终态等
  负向接受条件（纳入默认离线测试套件，无网络）。
- **范围边界**：示例只组合公开 Runner 与 SQLiteRunStore，不引入
  RAG、Session、Workflow、MultiAgent、LLM judge、UI 或 hosted
  service；最终结构化结果是确定性模型输出，不是 Output Repair。
