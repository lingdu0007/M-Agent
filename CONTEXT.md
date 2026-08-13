# M-Agent

M-Agent 是一个可嵌入 Python 应用的 Agent Application Runtime。本词汇表定义运行时及其扩展能力之间使用的统一领域语言。

## Language

**Agent Application Runtime（Agent 应用运行时）**：
承载 Agent 执行生命周期并为外部能力提供接入边界的运行库。它不拥有具体业务逻辑、知识检索内部实现、模型推理服务或分布式基础设施。
_Avoid_：Learning Agent Framework、Agent Platform、Agent Infra Platform、All-in-one Agent Framework

**Runtime Integrator（运行时集成者）**：
把 M-Agent 作为 Python Library 嵌入 AI 应用服务的研发工程师，是首版直接服务的使用者。
_Avoid_：Platform Administrator、Workflow Designer、End User

**Agent Definition（Agent 定义）**：
具有稳定标识和不可变版本的 Agent 行为与能力声明，不承载任何一次具体执行的运行状态。
_Avoid_：Agent Instance、Agent Run、Stateful Agent

**Definition Snapshot（定义快照）**：
Agent Run 首次开始时冻结的 Agent Definition 版本及其行为声明，是该 Run 后续步骤和恢复执行使用的不可变配置。
_Avoid_：Latest Agent Config、Mutable Agent、Run State

**Definition Registry（定义注册表）**：
上层应用注册并按 `definition_id` 与 `version` 精确解析 Agent Definition 可执行实现的边界；它不持久化或反序列化代码。
_Avoid_：Latest Definition Resolver、Code Store、Plugin Marketplace

**Model Adapter（模型适配器）**：
把供应商模型接口映射为运行时统一模型契约并声明其实际 Model Capabilities 的扩展能力。
_Avoid_：Model Provider、Model Client Wrapper、LLM Service

**Model Capabilities（模型能力集）**：
Model Adapter 对 streaming、tool calling、原生 structured output 和 usage reporting 等语义支持的明确声明。
_Avoid_：Provider Features、Best-effort Compatibility、Model Metadata

**Agent Instruction（Agent 指令）**：
由 Agent Definition 或上层应用受信控制接口提供的模型行为约束；外部上下文和工具结果不能成为 Agent Instruction。
_Avoid_：Context Item、Tool Result、Retrieved Instruction、Prompt Fragment

**Output Contract（输出契约）**：
Agent Definition 可选声明的版本化最终输出 Schema、验证规则和允许的 fallback；调用方不能在单次 Agent Run 中临时替换它。
_Avoid_：Run Schema、Ad-hoc Output Format、Prompt-only JSON

**Output Repair（输出修复）**：
模型结果不符合 Output Contract 后，由 Runner 按有界策略创建的新 Model Step；它不是原 Model Step 的重试。
_Avoid_：Model Retry、Parser Recovery、Silent Coercion

**Run Policy（运行策略）**：
在 Agent Run 生命周期的明确 Policy Gate 上执行的确定性约束，不隐藏模型调用或其他 Agent 执行。
_Avoid_：Guardrail、Hidden Agent、Prompt Rule

**Policy Gate（策略关口）**：
Run Policy 介入执行的位置，只能是 `INPUT`、`CONTEXT`、`TOOL_REQUEST`、`TOOL_OUTCOME` 或 `FINAL_OUTPUT`。
_Avoid_：Hook、Middleware Event、Run Step

**Policy Decision（策略决定）**：
Run Policy 在 Policy Gate 返回的结构化结果，只能是 `ALLOW`、`REJECT` 或 `REQUIRE_RESOLUTION`。
_Avoid_：Boolean Guardrail、Tool Outcome、Run Resolution

**Context Provider（上下文提供器）**：
在 Model Step 前确定性地为模型提供外部上下文的通用扩展能力，不暴露或拥有其背后的检索、存储或生成机制。
_Avoid_：Retriever、RAG Provider、Knowledge Base、Memory

**Context Item（上下文项）**：
Context Provider 返回的一条带稳定标识、内容、来源和提供方元数据的外部数据；运行时保留其顺序与溯源信息，但不解释检索分数或引用规则。
_Avoid_：Document Chunk、Retrieved Chunk、Raw Context、Prompt Fragment

**Agent Run（Agent 运行）**：
Agent Definition 针对一次输入产生的一次独立执行，是运行状态、步骤记录和执行结果的归属边界。
_Avoid_：Task、Invocation、Agent Instance、Conversation

**Runner（运行推进器）**：
按照 Agent Definition 推进或恢复 Agent Run 的嵌入式执行组件；它不负责进程部署、后台任务调度或服务托管。
_Avoid_：Worker、Scheduler、Agent Service、Job Queue

**Runtime Companion（运行时配套能力）**：
由 M-Agent 维护、建立在稳定运行时契约之上但不参与 Runner 核心循环的可选能力。
_Avoid_：Runtime Core、Runner Plugin、Agent Platform Service

**Local Content Adapter（本地内容适配器）**：
把只读 Project Content 作为 Tool 提供给 Agent Definition 的 Runtime Companion；它不提供写文件、Shell 或任意代码执行能力。
_Avoid_：Builtin Filesystem、Sandbox、Code Executor、Workspace Tool

**Run Status（运行状态）**：
Agent Run 在生命周期中的当前状态，只能是 `CREATED`、`RUNNING`、`WAITING`、`SUCCEEDED`、`REJECTED`、`FAILED` 或 `CANCELLED`；后四者为终态。
_Avoid_：Process Status、Worker Status、Step Status

**Run Resolution（运行处置）**：
上层应用针对 `WAITING` Agent Run 提交的显式控制决定，只能要求重试或确认当前 Tool Step，或将 Agent Run 终结为失败或取消。
_Avoid_：Model Decision、Tool Result Guess、Skip Step

**Cancellation Request（取消请求）**：
调用方要求 Runner 停止继续推进 Agent Run 的意图；它不代表 Run 已进入 `CANCELLED`，也不承诺撤销已经发生的外部副作用。
_Avoid_：Cancellation、Rollback、Cancelled Run

**Run Step（运行步骤）**：
Agent Run 中可独立记录结果并决定是否重试的最小执行边界。
_Avoid_：Workflow Step、Task Step、Trace Event

**Step Attempt（步骤尝试）**：
Run Step 的一次具体执行尝试；同一 Run Step 可以有多次尝试，未完成尝试产生的部分输出不构成该步骤的结果。
_Avoid_：Run Step、Retry Event、Partial Checkpoint

**Step Failure（步骤失败）**：
Model、Context Provider 或 Tool 适配器对一次失败 Step Attempt 的标准分类，只能是 `TRANSIENT`、`PERMANENT` 或 `UNCERTAIN`。
_Avoid_：Exception String、Tool Outcome、Run Status

**Retry Policy（重试策略）**：
Definition Snapshot 中针对 Run Step 声明的有界重试规则；未配置时 Runner 不自动重试。
_Avoid_：Error Handler、Tool Effect、Recovery Guarantee

**Model Step（模型步骤）**：
以一次模型请求及其完整响应为边界的 Run Step。
_Avoid_：Agent Turn、Reasoning Step、Model Event

**Tool Step（工具步骤）**：
以单个工具调用及其结果为边界的 Run Step；同一模型响应请求的多个工具调用分别形成各自的 Tool Step。
_Avoid_：Tool Batch、Tool Event、Function Step

**Context Step（上下文步骤）**：
以一次 Context Provider 调用及其完整结果为边界的只读 Run Step；已完成结果在恢复时复用，不为同一 Agent Run 静默重新获取。
_Avoid_：Retrieval Step、RAG Step、Memory Load、Context Event

**Run Store（运行存储）**：
持久化 Run Status 和 Run Step 记录的权威边界，是 Agent Run 恢复所依赖的唯一事实来源。
_Avoid_：Trace Store、Log Store、Memory、Session Store

**Run Metadata（运行元数据）**：
Run Store 中可直接查询的状态、时间、步骤类型、错误码和用量等非内容记录。
_Avoid_：Run Payload、Trace Data、Prompt

**Run Payload（运行载荷）**：
Agent Run 恢复所需的模型内容、Context Item、工具参数和结果；它与 Run Metadata 分离并经 Payload Codec 处理后持久化。
_Avoid_：Credential、Trace Body、Session Turn

**Payload Codec（载荷编解码器）**：
上层应用为 Run Store 配置的 Run Payload 序列化与保护边界；它不接收 API Key、访问令牌等运行凭据。
_Avoid_：Secret Store、Trace Redactor、Model Adapter

**Run Lease（运行租约）**：
Run Store 授予单个 Runner、允许其在有效期内推进某个非终态 Agent Run 的排他性权利；租约过期后其他 Runner 才能接管。
_Avoid_：Worker Assignment、Distributed Lock、Run Ownership

**Checkpoint（恢复点）**：
Run Store 中已经确认持久化、可供 Agent Run 在中断后继续执行的 Run Step 边界。
_Avoid_：Trace Event、Log Entry、Memory Snapshot

**Trace（运行轨迹）**：
用于诊断和观测 Agent Run 的非权威执行记录，可以被采样、脱敏或丢弃，不参与恢复决策。
_Avoid_：Checkpoint、Run Store、Audit Source of Truth

**Telemetry Sink（遥测接收器）**：
接收带 Run、Step 和 Attempt 关联标识的结构化 Trace 的轻量扩展边界；它默认不接收 Run Payload。
_Avoid_：Run Store、Observability Platform、Dashboard、Trace Context

**Run Update（运行更新）**：
Runner 面向上层应用发布的稳定实时通知，用于呈现 Agent Run 的状态、步骤和输出进展；它不是权威状态，丢失后由应用从 Run Store 重新读取当前事实。
_Avoid_：Trace Event、Checkpoint、Run Record

**Side-effecting Tool（副作用工具）**：
调用后可能改变运行时之外状态的工具。它必须向运行时声明自身的幂等能力或恢复策略。
_Avoid_：Unsafe Tool、Write Tool、External Tool

**Tool Effect（工具影响类型）**：
工具对外部状态影响及其重试安全性的声明，只能是 `READ_ONLY`、`IDEMPOTENT` 或 `NON_IDEMPOTENT`；未声明时按 `NON_IDEMPOTENT` 处理。
_Avoid_：Tool Risk、Permission Level、Retry Policy

**Tool Outcome（工具结果）**：
Tool Step 明确返回的 `SUCCESS` 或 `REJECTED` 结构化结果；未捕获异常属于 Step Attempt 失败，不是 Tool Outcome，也不直接提供给模型。
_Avoid_：Exception String、Tool Trace、Step Failure

**Session（会话）**：
共享连续对话上下文的一组 Agent Run；首版同一 Session 最多关联一个非终态 Agent Run，但 Session 本身不代表任何一次执行的运行状态。
_Avoid_：Agent Run、Conversation Run、Execution

**Session Store（会话存储）**：
持久化 Session 对话历史的权威边界，与保存单次执行状态的 Run Store 分离。
_Avoid_：Memory、Run Store、Long-term Memory、Knowledge Store

**Session Snapshot（会话快照）**：
Agent Run 首次开始时从 Session Store 冻结的带版本对话历史，是该 Run 在恢复及后续步骤中使用的不可变输入。
_Avoid_：Live Session、Memory Load、Run History、Checkpoint

**Session Turn（会话轮次）**：
成功 Agent Run 向 Session Store 提交的一组用户输入、最终输出和 Run 标识；中间模型响应、Context Item 与工具轨迹不属于 Session Turn。
_Avoid_：Run History、Agent Transcript、Trace、Memory Entry

**Project Root（项目根目录）**：
调用方为本地内容访问指定的唯一目录，它界定 Agent 可以发现和读取本地内容的范围。Project Root 可以由符号链接定位，但其范围由该链接指向的真实目录确定。
_Avoid_：Workspace Root、Repository Root、Allowed Directory

**Project Content（项目内容）**：
位于 Project Root 内且允许框架读取、搜索或索引的普通文件。符号链接不属于 Project Content，即使其目标仍在 Project Root 内。
_Avoid_：Filesystem Content、Linked Content、任意本地文件
