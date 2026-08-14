---
status: accepted
---

# 参考验收采用版本化多场景包而非巨型示例

M-Agent 以版本化 Reference Acceptance Pack 作为三个里程碑的统一参考验收形态：多个独立 Reference Scenario 分别通过构建后的公共 wheel 和公开 API 执行，每个场景拥有明确前置条件、正负路径、公开证据与声明的契约覆盖；共享 Harness Runner、Acceptance Manifest、Acceptance Coverage Matrix 和汇总报告，但任何场景失败都不能被其他场景成功或聚合分数掩盖。最小场景固定为 `core-lifecycle`、`durable-effects-recovery`、`session-conversation`、`context-budget-compression`、`model-routing` 与 `eval-regression`；Telemetry、凭据脱敏、发行物身份和负例是各相关场景的横向 required checks。现有 Durable Support Agent 保留并收敛为恢复与外部副作用场景，不再持续吸收其他 Companion 的全部证明；0.3、0.4、0.5 复用同一 Pack 的版本化子集，而不是复制三套脚本。

证据严格分为四层：`CONTRACT` 是纯离线 deterministic fixture 与公共 API 契约，`HOST` 是从干净临时环境安装构建 wheel 后使用真实子进程、SQLite、文件权限和进程重启的离线运行，二者共同构成通用库的可重复发布门槛；`PROVIDER` 是显式 opt-in、凭据门控且带时效的具体 live endpoint 合同，只证明该 Contract 在记录时间兑现能力；`FIELD` 由应用团队在真实部署、数据权限和外部系统中执行，只复用 Manifest 与证据 schema，不是通用库发布阻塞项。未授权的 live 检查可标记 `NOT_RUN`，但 CONTRACT fake 不能替代成 PROVIDER PASS，仓库也不能用 HOST 或 PROVIDER 结果宣称 FIELD/production 能力。单项只允许 `PASS`、`FAIL`、`ERROR`、`NOT_RUN` 或 `INCONCLUSIVE`；只有 Manifest 中全部 required CONTRACT/HOST 为 PASS 才是 PASSED，required NOT_RUN/INCONCLUSIVE 使单个 Pack 为 INCOMPLETE，optional 不影响也不提升结论。

`ERROR` 有两层且不可混淆的语义。单个 `PackExecution` 保留 required check 的 `ERROR`，状态为 `ERROR`、退出码为 `3`，不得把 harness 或证据错误伪装为被测 subject 的 `FAILED`。后续 Release 或 Milestone 汇总可把任一 required `FAIL` 或 `ERROR` 投影为 aggregate `FAILED`，但这是上层发布结论，绝不回写或改写子 `PackExecution` 的状态、退出码或 Bundle。

三个里程碑采用递增 Pack Profile 并重跑所有既有 required Scenario：0.3 要求 `core-lifecycle` 与 `durable-effects-recovery`，0.4 追加 `session-conversation` 与 `context-budget-compression`，0.5 再追加 `model-routing` 与 `eval-regression`。`foundation-release` Profile 在同一 RC wheel、Manifest、environment 和 Pack execution id 下按依赖顺序运行六个隔离场景；每个场景使用独立 Store 和 execution id，Eval 只消费前序 Scenario Evidence Bundle 的公开视图，不能读取其私有数据库或日志。不同 artifact/Manifest 的结果不得拼接，正式修复验收必须在新 RC 上完整重跑；旧 Bundle 只用于诊断，报告渲染或后来获准的 PROVIDER 证据只能形成显式新 revision。

HOST 必须从干净 checkout 构建并记录 source commit、dirty state、构建工具与 sdist/wheel digest，在仓库外新建虚拟环境并从本次 wheel 安装，禁止 editable install、`PYTHONPATH=src` 或源码目录导入；运行记录 distribution/version、Python、OS、架构、依赖摘要和 Manifest digest。dirty checkout 只能产生 DEVELOPMENT 证据，不能成为 RC PASSED。Linux Python 3.11-3.14 运行 required CONTRACT，Linux 3.11 是完整 primary HOST；macOS 最低与最高支持 Python 在 0.3 可作为明确 release gap，但 0.5 前必须成为 secondary HOST required。Windows 当前明确不支持，不以 NOT_RUN 暗示承诺。

Harness 以冻结 Manifest、关键结论双源证据和变异自检保持独立性：每项稳定契约映射 owner、public seam、正负检查、权威/独立证据、required level、milestone 和 non-claim；缺映射即 INCOMPLETE，覆盖率不作通过门槛。每个 Scenario 至少一个受控变异必须使目标 check 失败，否则 Harness 为 ERROR。跨进程恢复使用按公开生命周期命名的 Failure Script，Adapter/Store fixture 在外部边界写 durable sentinel 后以约定退出码终止真实子进程，新进程只通过公开 resume/reconcile/inspection 继续；required 故障窗口连续重复三次，随机 kill 只能是 optional 压力证据。

每个 Scenario 产出不可变、内容寻址且最小脱敏的 Scenario Evidence Bundle，绑定 Manifest、execution、checks、公开 Evidence View、独立外部证据和完整性摘要，JSON 是权威数据，Markdown 只是渲染；缺失、过期、schema 或 hash 不匹配不能解释成“问题不存在”。报告位于 RunStore、SessionStore 与 Eval Store 之外且写入失败不改写被测状态。Reference Pack 只提供 append-only journal、内容寻址 snapshot file 和 sentinel 三类离线 Evidence Adapter 及 contract test kit，真实业务系统 Adapter 由垂直项目在 FIELD 层实现。

Telemetry required checks 只验证公开事件契约并与权威 Inspection 对账，Store 仍是执行事实权威，外部效果仍由独立 Evidence 证明。JSONL Sink 在 CONTRACT/HOST 验证关联、顺序、usage 来源、close/flush、跨进程完整性、并发和脱敏；OpenTelemetry Adapter 从 0.3 起有 required CONTRACT 和 optional HOST 本地 exporter 检查，真实 Collector 属于 FIELD。安全横向门槛验证 credential 隔离、测试专用加密 PayloadCodec 的落盘/密钥 fail-closed、Scope 隔离、Instruction/Data 边界、Eval OBSERVE 只读、外部 effect dispatch 权限与 RC/Manifest 完整性，但不宣称业务合规、模型免疫 prompt injection、供应商治理合规或供应链认证等级。

性能 benchmark 是 HOST 附件而非功能 Scenario，必须先通过 Run/Step/Checkpoint/effect/database 完整性再输出环境限定的 throughput、P50/P95、持久化开销和资源数据；release 只比较兼容环境下已批准 baseline 的相对回退，不设跨机器通用阈值或宣称生产 QPS/SLA/容量。0.3 保留固定 100-run SQLite workload，后续 Session/Context/Eval workload 分别建 baseline，不能合成单一吞吐数字。

官方 live Adapter 在协议序列化、能力、Model Contract 或归一化变化时，受影响 endpoint/capability 必须在 RC 上重跑 PROVIDER 并 PASS；无关变更可复用 fingerprint 匹配且不超过 30 天的证据，过期、alias 漂移或供应商变化标为 STALE，默认推荐 Catalog 项的 stale evidence 阻塞发布。`MISSING_CREDENTIALS`、`OPTED_OUT`、`QUOTA_UNAVAILABLE`、`PROVIDER_FAILURE`、`CONTRACT_FAILURE` 与 `HARNESS_ERROR` 分开报告，Bundle 只保存最小能力/usage/latency 摘要与 digest。

Harness 属于同一 `m-agent` distribution 的 `m_agent.testing`/`testing` extra，Core 不反向依赖；薄 CLI 默认只运行离线 CONTRACT/HOST，PROVIDER 必须用独立命令显式 `allow-live`。Pack Execution 绑定唯一 source/artifact/Manifest/environment，状态为 `CREATED`、`RUNNING`、`PASSED`、`FAILED`、`INCOMPLETE` 或 `ERROR`；恢复只续跑未完成场景，半成品 Scenario 重跑。每个里程碑最终还须在当前 checkout 的 tracked/staged/unstaged/untracked 全视图及构建 wheel 上完成独立 Standards/Spec 双轴 Review与公共 seam 定向探针；任何 P0/P1/P2、required INCOMPLETE 或 Ticket/ADR/实现不一致均阻塞，另一 worktree 的证据不可移植。

面试演示固定为 12-15 分钟：先展示 Core/Adapter/Companion/Testing 单向边界，再现场执行 durable recovery，随后用确定性 Bundle 展示 Context 超预算零 dispatch、结构化路由失败和 Eval hard gate，最后解释四层证据与 non-claim；现场前必须已有完整 foundation-release PASSED Bundle，现场失败只能诚实切换到预生成不可变证据，不使用 live provider 或凭据。该设计增加了场景编排、Manifest 和证据治理成本，却能隔离失败、展示责任边界，并避免单一 happy path、较低层证据或绿色测试数量成为夸大适用范围的伪验收。
