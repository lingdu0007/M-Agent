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

**Runtime Adapter（运行时适配器）**：
实现 Runtime Core 某一个明确扩展契约、把外部模型、存储、遥测或载荷保护能力接入该契约边界的具体集成；它不组合更高层运行能力，也不改变 Runner 执行语义。
_Avoid_：Runtime Companion、Runner Plugin、Middleware、Core Module

**Model Adapter（模型适配器）**：
把供应商模型接口映射为运行时统一模型契约并声明其实际 Model Capabilities 的扩展能力。
_Avoid_：Model Provider、Model Client Wrapper、LLM Service

**Model Capabilities（模型能力集）**：
Model Contract 中可静态校验的类型化语义支持声明，例如 streaming、tool calling、原生 structured output 和 usage reporting 的具体模式及可用组合；未声明组合默认不支持，它不表达容量上限或经验质量。
_Avoid_：Boolean Feature Bag、Provider Features、Best-effort Compatibility、Model Metadata

**Model Contract（模型契约）**：
Model Adapter 提供并由 Definition Snapshot 冻结的版本化模型执行契约，组合稳定身份、修订稳定性、Model Capabilities、Model Limits、Model Input Sizer 标识和稳定非敏感指纹；它不包含凭证、动态价格或经验评分。
_Avoid_：Provider Config、Model Catalog Entry、Routing Policy、Eval Result

**Model Limits（模型限制）**：
Model Contract 对上下文窗口、最大输出等可确定计量边界的定量声明，供 Model Requirements 与 Context Budget 校验；它不以 `long_context` 布尔标签代替实际容量。
_Avoid_：Context Budget、Quality Score、Provider Quota、Long-context Badge

**Usage Field Guarantee（用量字段保证）**：
Model Contract 对 input、output、cached input、reasoning token 等各用量字段分别声明 `REQUIRED`、`OPTIONAL` 或 `UNSUPPORTED` 的契约强度；未声明或缺失时不能伪造精确用量。
_Avoid_：Usage Boolean、Cost Estimate、Provider Invoice、Best-effort Usage

**Model Requirements（模型需求）**：
Agent Definition 声明并随 Definition Snapshot 冻结的模型最低语义能力、定量容量和输出要求；它只表达 Agent 正确运行所需条件，不承载价格、供应商或质量偏好。
_Avoid_：Routing Policy、Provider Allowlist、Model Recommendation、Best Model

**Model Binding Set（模型绑定集）**：
Agent Definition 按 `PRIMARY`、`CONTEXT_COMPRESSION`、`OUTPUT_REPAIR` 等用途显式绑定并冻结的 Model Requirements 与 Model Contract 集合；每个用途独立声明，引用 `PRIMARY` 也必须显式，Judge Model 不属于被评估 Run 的绑定集。
_Avoid_：Single Model Field、Dynamic Step Routing、Judge Configuration、Implicit Inheritance

**Model Execution Budget（模型执行预算）**：
Definition Snapshot 中对整个 Agent Run 及各 Model Binding 用途可 dispatch 的 Model Step Attempt 次数设定的持久化硬上限；每次外部调用前原子预留，失败或结果不确定也消费额度，恢复不会返还。
_Avoid_：Retry Policy、Pricing Policy、Provider Quota、Successful-call Counter

**Model Evidence（模型证据）**：
Eval 或运行观测对结构化输出可靠性、工具成功率、长上下文效果、延迟和成本等经验性质产生的版本化证据；它不能由 Model Adapter 自我声明为硬能力。
_Avoid_：Model Capability、Marketing Claim、Mutable Score、Adapter Metadata

**Agent Variant（Agent 变体）**：
同一评估或业务目标下可独立注册、选择和比较的完整版本化 Agent Definition，绑定确定的 Model Contract；它不是已启动 Run 内可替换的模型参数。
_Avoid_：Mutable Definition、Model Alias、Run Override、Routing Candidate ID

**Model Catalog（模型目录）**：
Runtime Companion 中组织已注册 Agent Variant 及其静态 Model Contract 的选择视图；它不持有模型凭证，也不参与 Runner 执行循环。
_Avoid_：Definition Registry、Secret Store、Provider Marketplace、Runner Registry

**Deployment Constraints（部署约束）**：
Routing Policy 对 provider、region、endpoint class、数据保留证据或应用准入范围施加的硬选择条件；它不属于模型推理能力，也不包含凭据或敏感 endpoint 配置。
_Avoid_：Model Capabilities、Credential Policy、Compliance Guarantee、Secret Config

**Routing Policy（路由策略）**：
Runtime Companion 中版本化的 Run 前选择规则，组合 Model Requirements、候选范围及成本、延迟、质量等偏好；它不改变 Agent Definition 的运行语义。
_Avoid_：Run Policy、Retry Policy、Definition Snapshot、Provider Load Balancer

**Model Router（模型路由器）**：
在 `create_run` 前依据 Model Catalog、Routing Policy 和 Model Evidence 选择完整 Agent Variant 的 Runtime Companion；Runner 不调用它，已启动 Agent Run 也不由它换模。
_Avoid_：Runner、Model Adapter、In-run Fallback、Dynamic Definition Resolver

**Routing Decision（路由决定）**：
Model Router 对一次 Run 前选择产生的不可变证据，记录最终 Agent Variant、Model Contract 指纹、候选过滤 reason code，以及所用 policy、evidence、价格和可用性快照版本；Core 只关联其标识与摘要，Definition Snapshot 仍是执行真相。
_Avoid_：Definition Snapshot、Model Response、Mutable Route、Provider Request

**Routing Result（路由结果）**：
Model Router 在任何 Run 或 Session Claim 创建前返回的结构化结果，只能是成功选择或明确的兼容性、策略、证据、Catalog、Policy 失败；失败不触发模型请求且不隐式放宽规则。
_Avoid_：Exception Text、Fallback Model、Rejected Agent Run、Session Result

**Pricing Snapshot（价格快照）**：
带来源、币种、计价单位、生效时间和有效期的版本化模型价格证据，供 Run 前成本估算和事后费用计算；它不是 Model Contract，也不承诺供应商账单金额。
_Avoid_：Model Limit、Usage Report、Invoice、Hard-coded Price

**Availability Snapshot（可用性快照）**：
应用或只读探针提供的版本化模型 endpoint 状态证据，记录来源、范围、采样时间和有效期；Router 只消费它而不发起隐藏模型探测，它也不保证 dispatch 时仍可用。
_Avoid_：Active Routing Probe、Circuit Breaker、Availability Guarantee、Run Retry

**Operational Limits Snapshot（运行限额快照）**：
应用或 Runtime Companion 提供的 RPM、TPM、并发数、账户余额或周期配额等动态证据；Router 可在 Run 前消费它，但 Core 不把它冻结为模型语义或拥有跨 Run 计数器。
_Avoid_：Model Limits、Context Budget、Quota Service、Rate-limit Guarantee

**Model Recommendation（模型建议）**：
Eval 基于不可变 Report/Baseline 产生的版本化模型选择建议，记录适用目标、Variant、hard gate、质量/成本/延迟证据、置信度和有效期；必须经应用显式发布为新 Routing Policy 或 Agent Variant 才影响后续 Run。
_Avoid_：Automatic Promotion、Routing Decision、Mutable Leaderboard、Definition Update

**Model Fallback（模型降级选择）**：
Model Router 在 Agent Run 创建前因候选不满足能力、成本或可用性策略而按冻结顺序选择下一完整 Agent Variant；它不表示 dispatch 后在原 Run 内切换模型。
_Avoid_：Model Retry、Run Recovery、Provider Failover、Output Repair

**Replacement Run（替代运行）**：
应用在已启动 Agent Run 无法继续使用原 Model Contract 时，以另一 Agent Variant 显式创建并关联的新 Agent Run；新 Run 不继承原 Run 的身份、Checkpoint 或重试次数。
_Avoid_：Retry Attempt、Resumed Run、In-place Model Switch、Fallback Step

**Run Cost Estimate（运行成本估算）**：
Runtime Companion 基于 Pricing Snapshot、完整请求计量和预留输出产生的有证据范围或最坏情况估算；缺少可靠 usage 或价格时不能宣称为精确实际费用。
_Avoid_：Provider Invoice、Account Quota、Exact Cost Without Usage、Context Budget

**Model Contract Violation（模型契约违约）**：
具体模型或 Adapter 在 dispatch 后未兑现冻结 Model Contract 的协议保证，例如拒绝已声明模式、返回无法归一化的协议组合或缺少 `REQUIRED` usage 字段；它不是业务 Output Contract 验证失败或临时限流。
_Avoid_：Output Validation Failure、Unsupported Candidate、Transient Provider Failure、Eval Quality Failure

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
Context Provider 或 Context Stage 返回的一条不可变外部数据，具有 Run 内稳定标识、内容、来源及对直接输入项的派生引用；运行时保留顺序与溯源关系，但不解释检索分数或变换算法。
_Avoid_：Document Chunk、Retrieved Chunk、Raw Context、Prompt Fragment

**Context Stage Result（上下文阶段结果）**：
一次 Context Stage 执行的结构化输出，记录输入引用、输出 Context Item、变换决定、计量与稳定变换类型，是该阶段 Checkpoint 和 Eval 检查的证据边界。
_Avoid_：Context Item List、Trace Event、Prompt、Model Response

**Context Frame（上下文帧）**：
一次业务 Model Step 使用的分区化上下文视图，由 Run Input 基础项、按 Tool Step 累积的增量项和当前 Model Step 工作项组成；它不把 Tool Outcome 伪装成 Context Item。
_Avoid_：Global Context List、Prompt、Session Snapshot、Tool Transcript

**Context Budget（上下文预算）**：
一次 Model Step 的完整模型请求及预留输出允许消费的资源上限；它约束指令、输入、历史、Context Item、Tool Outcome 和协议结构的总规模，但不规定筛选算法。
_Avoid_：Max Prompt Length、Model Limit、Token Counter

**Model Input Sizer（模型输入计量器）**：
按照 Model Adapter 的实际请求契约，对完整 Model Request 及其组成部分给出精确或保证不低估计量的扩展边界；它不负责选择或裁剪上下文。
_Avoid_：Context Sizer、Character Counter、Usage Report、Tokenizer in Core

**Context Pipeline（上下文管线）**：
按确定顺序把外部数据准备、筛选、变换并交付给 Model Step 的组合边界；它保留 Context Item 的来源与可追溯关系。
_Avoid_：Retriever Chain、Prompt Builder、RAG Pipeline

**Context Plan（上下文计划）**：
Agent Definition 声明并随 Definition Snapshot 冻结的有序 Context Stage 序列及其作用域，是 Core 可恢复推进 Context Pipeline 的不可变执行声明。
_Avoid_：Workflow、Dynamic Pipeline、Provider Implementation、Prompt Template

**Context Stage（上下文阶段）**：
Context Plan 中具有稳定身份、明确输入输出和独立恢复证据的最小上下文处理单元；具体筛选、排序、去重或裁剪算法不属于 Core。
_Avoid_：Run Step、Hidden Hook、Workflow Node、Model Call

**Context Scope（上下文作用域）**：
Context Pipeline 结果在 Agent Run 生命周期中的适用范围，用于区分整次 Run 共享的稳定上下文与随执行进展重新准备的动态上下文。
_Avoid_：Cache Lifetime、Retrieval Frequency、Prompt Scope

**Semantic Compression（语义压缩）**：
依据版本化 Compression Contract，把 Context Item 转换为保留来源引用和显式省略信息的更紧凑派生项的有损处理；调用模型时它必须形成独立 Model Step，不能静默修改原始项。
_Avoid_：Truncation、Summarization by Default、Lossy Prompt Rewrite

**Eval Case（评估用例）**：
固定输入、Agent Definition/Variant、Fixture Bundle、Execution Protocol 和 Evaluator 版本的一项不可变评估声明，是回归比较的最小场景单位。
_Avoid_：Test Script、Agent Run、Ad-hoc Prompt、Mutable Scenario

**Eval Suite（评估套件）**：
把版本化 Eval Case、Agent Variant、重复策略和聚合策略组合成一次可展开评估计划的边界；它不在已启动 Run 中替换模型或配置。
_Avoid_：Benchmark Score、Workflow、Model Router、Test Folder

**Evaluator（评估器）**：
只读 Eval Observation Projection 和 Evidence Artifact 并产生结构化断言或指标的版本化规则；外部读取必须走 Evidence Adapter，模型判断必须走独立 Judge Run。
_Avoid_：Run Policy、Production Hook、Judge Prompt、Mutable Assertion

**Eval Observation（评估观测）**：
Eval Companion 从受控测试 Run 或只读已有 Run 归一化得到的不可变评估输入，关联执行、上下文、工具、策略、恢复、用量及外部证据来源，但不改变被评估 Run。
_Avoid_：Run Store、Eval Report、Trace Dump、Production Mutation

**Observation Projection（观测投影）**：
依据版本化授权与脱敏策略，从 Eval Observation 中最小化提取某个 Evaluator 所需证据的只读视图；证据未授权或不可用时不能绕过边界读取。
_Avoid_：Full Run Payload、Prompt Dump、Evaluator Input Without Policy

**Eval Report（评估报告）**：
Eval Store 中不可变、可修订的评估结果，固定 Suite、Case、Variant、Evaluator、证据、环境和聚合版本；导出的 JSON、Markdown 或 HTML 只是它的视图。
_Avoid_：Run Record、Mutable Dashboard、Production Run State、Report File as Source of Truth

**Eval Execution（评估执行）**：
Eval Store 中记录一次 Suite 展开、调度、恢复和报告进度的持久化编排事实；它通过稳定 item 与 Agent Run 关联，但不创造新的 Run Status。
_Avoid_：Agent Run、Workflow Run、Background Job、Eval Report

**Eval Baseline（评估基线）**：
显式引用某个不可变 Eval Report revision 及比较策略的版本化回归参照；它不会因最新执行、价格或 Judge 变化而自动更新。
_Avoid_：Latest Report、Moving Average、Automatic Release Target、Mutable Score

**Evidence Artifact（证据制品）**：
Evidence Adapter 从 Run Store 外部只读收集并冻结的版本化评估证据，带来源、主体引用、schema 和完整性摘要；缺失不能被解释为外部效果不存在。
_Avoid_：Arbitrary File、Production Database Handle、Trace Text、Evaluator Side Effect

**Agent Run（Agent 运行）**：
Agent Definition 针对一次输入产生的一次独立执行，是运行状态、步骤记录和执行结果的归属边界。
_Avoid_：Task、Invocation、Agent Instance、Conversation

**Runner（运行推进器）**：
按照 Agent Definition 推进或恢复 Agent Run 的嵌入式执行组件；它不负责进程部署、后台任务调度或服务托管。
_Avoid_：Worker、Scheduler、Agent Service、Job Queue

**Runtime Companion（运行时配套能力）**：
由 M-Agent 维护、建立在稳定运行时契约之上但不参与 Runner 核心循环的可选能力。
_Avoid_：Runtime Core、Runner Plugin、Agent Platform Service

**Reference Acceptance Pack（参考验收包）**：
由 M-Agent 维护、通过构建后公共发行物运行的一组版本化 Reference Scenario、Acceptance Manifest 与汇总证据，用于证明 Runtime Core/Companion 契约可组合且发布门槛可复现；它不是业务 Agent、生产容量认证或单一 happy-path 示例。
_Avoid_：Demo Agent、Test Suite、Benchmark、Production Certification

**Reference Scenario（参考场景）**：
Reference Acceptance Pack 中独立运行、具有明确前置条件、公开入口、正负路径和证据边界的最小组合旅程；每个场景只证明声明的契约集合，不以其他场景的成功掩盖自身失败。
_Avoid_：Unit Test、User Story、Business Workflow、Acceptance Check

**Acceptance Manifest（验收清单）**：
冻结 Reference Acceptance Pack 版本、Scenario、发行物身份、环境要求、证据类别、预期检查和里程碑门槛的机器可读声明；汇总器按它判定完整性，不能根据本次运行结果动态删减 required 检查。
_Avoid_：Test Discovery、CI Workflow、Mutable Checklist、Eval Report

**Acceptance Evidence Level（验收证据层级）**：
Reference Acceptance Pack 对证据适用范围的固定分类：`CONTRACT` 为离线确定性公共契约，`HOST` 为干净环境中安装 wheel 后的真实进程/存储运行，`PROVIDER` 为显式授权的具体 live endpoint 合同，`FIELD` 为应用团队真实部署与外部系统验收；低层证据不能升级替代高层结论。
_Avoid_：Test Pyramid、Environment Name、Confidence Score、Release Stage

**Acceptance Check Result（验收检查结果）**：
Reference Scenario 中一项声明检查的结构化结论，只能是 `PASS`、`FAIL`、`ERROR`、`NOT_RUN` 或 `INCONCLUSIVE`，并携带稳定 reason code 与 Evidence 引用；optional 成功不能覆盖 required 非通过结果。
_Avoid_：Boolean Test Result、Weighted Score、Exception Text、Release Verdict

**Failure Script（故障脚本）**：
Acceptance Manifest 中按公开生命周期语义冻结的确定性故障窗口与恢复期望，用于通过真实进程终止和公开恢复接缝证明 durable invariant；它不是随机压力测试或 Runner 私有 hook。
_Avoid_：Chaos Test、Private Failpoint、Mock Exception、Manual Database Edit

**Scenario Evidence Bundle（场景证据包）**：
一次 Reference Scenario Execution 产生的不可变、内容寻址、最小脱敏证据集合，绑定发行物、Manifest、环境、检查结果、公开 Evidence View 与完整性摘要；渲染报告不是其权威来源。
_Avoid_：Log Directory、Run Store Backup、Prompt Dump、Mutable Report

**Acceptance Coverage Matrix（验收覆盖矩阵）**：
把每项稳定契约的 owner、Reference Scenario、public seam、正负检查、权威/独立证据、required level、milestone 与 non-claim 精确关联的发布清单；覆盖率只供导航，缺少 required 映射即为不完整。
_Avoid_：Feature Checklist、Test Count、Coverage Percentage Gate、Requirements Summary

**Pack Profile（验收包配置）**：
Acceptance Manifest 中针对某一里程碑冻结的 Scenario 集合、依赖顺序、环境和门槛；`foundation-release` 在同一发行候选上组合独立 Scenario 结果，不创造共享状态的超级场景。
_Avoid_：Workflow、Mega Scenario、CI Job、Release Branch

**Pack Execution（验收包执行）**：
Reference Acceptance Pack 对一个精确 source commit、artifact digest、Manifest digest 和 environment profile 的持久化执行事实，状态只能是 `CREATED`、`RUNNING`、`PASSED`、`FAILED`、`INCOMPLETE` 或 `ERROR`；不同发行候选的结果不能拼接为一次通过。
_Avoid_：Agent Run、Eval Execution、Latest Green Build、Merged Test Report

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
以一次 Context Stage invocation 及其完整 Context Stage Result 为边界的 Run Step；已完成结果在同一 Scope trigger 恢复时复用，不静默重新获取或变换。
_Avoid_：Retrieval Step、Pipeline Run、RAG Step、Memory Load、Context Event

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

**Conversation History（对话历史输入）**：
Agent Run 创建时显式提供并冻结的有序用户/助手消息，是 Core 可持久化和恢复的模型输入；它不包含 Session 身份、Session 版本或 Session Store 行为。
_Avoid_：Session Snapshot、Memory、Live Conversation、Agent Instruction

**Session（会话）**：
共享连续对话上下文的一组 Agent Run；首版同一 Session 最多关联一个非终态 Agent Run，但 Session 本身不代表任何一次执行的运行状态。
_Avoid_：Agent Run、Conversation Run、Execution

**Session Scope（会话作用域）**：
上层应用随 SessionStore 操作提供的不透明授权与隔离上下文，用于限定一个 `session_id` 可被谁读取或修改；它不是用户、租户或模型输入。
_Avoid_：User ID、Tenant ID、RBAC Role、Session ID

**Session Store（会话存储）**：
持久化 Session 对话历史的权威边界，与保存单次执行状态的 Run Store 分离。
_Avoid_：Memory、Run Store、Long-term Memory、Knowledge Store

**Session Run Claim（会话运行占用声明）**：
Session Store 中指向一个 Agent Run 的持久化声明，用于保证同一 Session 同时最多由一个非终态 Agent Run 占用；它不是 Run Lease，也不代表进程所有权。
_Avoid_：Session Lock、Worker Assignment、Run Lease、Conversation Owner

**Session Snapshot（会话快照）**：
SessionRunner 在 Agent Run 创建前从 Session Store 读取的带版本完整对话历史；它由 Companion 转换成 Core 可持久化的 Conversation History。
_Avoid_：Live Session、Memory Load、Run History、Checkpoint

**Session Turn（会话轮次）**：
SessionRunner 为成功 Agent Run 向 Session Store 提交的一组用户输入、最终输出和 Run 标识；中间模型响应、Context Item 与工具轨迹不属于 Session Turn。
_Avoid_：Run History、Agent Transcript、Trace、Memory Entry

**Project Root（项目根目录）**：
调用方为本地内容访问指定的唯一目录，它界定 Agent 可以发现和读取本地内容的范围。Project Root 可以由符号链接定位，但其范围由该链接指向的真实目录确定。
_Avoid_：Workspace Root、Repository Root、Allowed Directory

**Project Content（项目内容）**：
位于 Project Root 内且允许框架读取、搜索或索引的普通文件。符号链接不属于 Project Content，即使其目标仍在 Project Root 内。
_Avoid_：Filesystem Content、Linked Content、任意本地文件
