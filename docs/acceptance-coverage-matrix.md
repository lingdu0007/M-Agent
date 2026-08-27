# Acceptance Coverage Matrix

This matrix is the frozen contract index for the 0.5 Runtime Foundation
candidate. Its release profile is `foundation-release-0-5` at Pack version
`foundation-release-0-5-v1` and it freezes all six required Scenarios under
one release-candidate Manifest identity: the 0.3 Runtime Baseline
(`core-lifecycle` and `durable-effects-recovery`) and the 0.4 Session and
Context Scenarios (the `foundation-release-0-4` profile;
`session-conversation` and
`context-budget-compression`) are rerun as-is under the same RC identity, and
`model-routing` and `eval-regression` add CONTRACT and HOST evidence. Every
row, including its owner, public seam, positive/negative assertion, evidence,
milestone, and non-claim, is frozen in a passing Manifest. The Testing CLI
rejects a Manifest that differs from this complete set; a row cannot be
omitted based on an execution result, and an older release-candidate Bundle
cannot attest this candidate.

The same Manifest freezes `required_cli_commands` as `run`, `inspect`,
`verify`, and `render`. This is a required public command mapping, rather than
a check result: `inspect`, `verify`, and `render` operate on a completed
Bundle and are proved by the external-wheel contract after `run`; a Bundle
therefore never attests to its own later verification or rendering.

| Check ID | Owner | Scenario and public seam | Positive and negative check | Authority and independent evidence | Level / milestone | Non-claim |
| --- | --- | --- | --- | --- | --- | --- |
| `core.lifecycle` | Runtime Core | `core-lifecycle`; `m_agent.runtime.Runner` | Create, start, inspect one deterministic Run; a non-success terminal is `FAIL`. | Public `RunInspection` counts, packaged fixture digest, and isolated wheel-process observation. | CONTRACT / Foundation | Does not prove provider or production execution. |
| `core.lifecycle.telemetry` | Telemetry Adapter | `core-lifecycle`; `m_agent.runtime.TelemetrySink`, `m_agent.adapters.JsonlTelemetrySink`, `OpenTelemetryTelemetrySink` | Ordered JSONL correlates Run/Step/Attempt, purpose/status/error/duration/usage and reconciles to public Inspection; the local exporter maps Run/Step/Attempt event spans, and the application-owned `Tracer.start_span` bridge preserves explicitly supplied upper trace context; payload/credential leakage or an unreconciled event is `FAIL`. | Public Inspection correlation and independent append-only JSONL digest. | CONTRACT / 0.3 | Does not prove provider endpoint, external Collector, or production observability. |
| `core.lifecycle.unknown-definition` | Runtime Core | `core-lifecycle`; `m_agent.runtime.DefinitionRegistry` | An unknown Definition raises the public error; successful resolution is `FAIL`. | Public Registry result and isolated wheel-process observation digest. | CONTRACT / Foundation | Does not prove Definition persistence. |
| `core.lifecycle.public-namespaces` | Distribution API | `core-lifecycle`; `m_agent.runtime`, `m_agent.adapters`, `m_agent.companion`, `m_agent.testing` | All four public namespaces import; a missing or wrong public binding is `FAIL`. | Installed wheel import view and isolated wheel-process observation digest. | CONTRACT / Foundation | Does not prove future Companion capabilities. |
| `core.lifecycle.dependency-direction` | Runtime Core | `core-lifecycle`; `m_agent.testing.find_runtime_dependency_violations` | The installed Core has no reverse layer import; a forbidden import is `FAIL`. | Static installed-Core source scan and isolated wheel-process observation digest. | CONTRACT / Foundation | Does not prove dynamic behavior outside public Core files. |
| `core.lifecycle.migration` | Distribution API | `core-lifecycle`; `m_agent`, `m_agent.runtime`, `m_agent.adapters` | The 0.3 root facade is exactly the documented subset and removed imports fail with directional migration errors; a legacy import that succeeds is `FAIL`. | Runtime public import view and independent migration-table digest. | CONTRACT / 0.3 | Does not claim backward-compatible 0.2 root retention. |
| `core.lifecycle.bundle-tamper` | Testing | `core-lifecycle`; `m_agent.testing.ScenarioEvidenceBundle` | A controlled changed Bundle must fail integrity verification; an undetected mutation is a Harness error. | Content digest and digest derived from the actual rejected mutation payload. | CONTRACT / Foundation | Does not prove external ledger integrity. |
| `core.lifecycle.host-wheel` | Testing | Clean external venv; `python -I -m m_agent.testing` | Exact built wheel and sdist install without editable mode, source path injection, or private imports; the Pack itself rejects controlled source/artifact/sdist/fixture/environment identity mutations. | Wheel and sdist SHA-256, all installed wheel members, packaged fixture bytes, build-tool version, Testing dependency summary, source-subject summary, mutation rejection digest, and isolated SQLite restart observation. | HOST / Foundation | Does not prove live provider behavior. |
| `core.lifecycle.telemetry-host` | Telemetry Adapter | Installed `m_agent.adapters.JsonlTelemetrySink`; `python -I -m m_agent.testing` | Wheel-installed JSONL survives close/reopen and reconciles with public SQLite Inspection; credential inheritance, plaintext, or process-integrity failure is `FAIL`. | Public installed-wheel Inspection and independent JSONL/process observation digest. | HOST / 0.3 | Does not prove external Collector or live provider behavior. |
| `durable.effects.recovery-windows` | Runtime Core | `durable-effects-recovery`; `Runner.resume_run`, `Runner.inspect_run` | Real subprocess hard exits after model reservation, effect dispatch, and before final model checkpoint; SQLite reopen repeated 3 times per window has no duplicate effect or budget reset. | Public `RunInspection` and independent journal/sentinel digest. | CONTRACT / 0.3 | Does not prove exactly-once external effects. |
| `durable.effects.budget-fail-closed` | Runtime Core | `durable-effects-recovery`; `ModelExecutionBudget` | A consumed reservation remains consumed after reopen and fails closed at the configured limit; silent budget reset is `FAIL`. | Public attempt/budget view and independent recovery journal. | CONTRACT / 0.3 | Does not claim provider token accounting. |
| `durable.effects.waiting-resolution` | Runtime Core | `durable-effects-recovery`; `RunResolution.confirm_step` | Uncertain non-idempotent effect remains `WAITING` until explicit resolution, repeated three times; auto-success is `FAIL`. | Public `WAITING`/resolution view and independent sentinel/journal evidence. | CONTRACT / 0.3 | Does not claim business approval semantics. |
| `durable.effects.mutation` | Testing | `durable-effects-recovery`; `reconcile_recovery_window` | A controlled duplicate-effect mutation must fail reconciliation. | Runtime reconciliation result and independent mutation digest. | CONTRACT / 0.3 | Does not prove external ledger integrity. |
| `durable.effects.host-wheel` | Testing | Clean external venv; installed wheel and `SQLiteRunStore` | Exact wheel runs the durable crash/reopen probe; source or artifact mismatch is `FAIL`. | Installed-wheel observation and independent journal/sentinel digest. | HOST / 0.3 | Does not prove live provider or production effect behavior. |
| `session.conversation.recovery-windows` | Session Companion | `session-conversation`; `SessionRunner.resume`, `SQLiteSessionStore`, `Runner.get_run` | Three deterministic cross-store crash windows (after claim, after partial turn commit, after core terminal) reopen and recover repeatedly; a partial commit or a rewritten core terminal is `FAIL`. | Public session/run view digest and independent recovery journal digest. | CONTRACT / 0.4 | Does not prove exactly-once external effects. |
| `session.conversation.claim-no-ttl` | Session Companion | `session-conversation`; `SessionStore.claim_run`, `SessionStore.get_claim` | Restart and stale owner keep exactly one active session claim; a silent second run admission is `FAIL`. | Public claim view digest and independent journal digest. | CONTRACT / 0.4 | Does not claim time-based claim recovery. |
| `session.conversation.payload-protection` | Session Companion | `session-conversation`; `SQLiteSessionStore`, `PayloadCodec` | An independent session codec protects history bytes at rest and a wrong key fails closed; plaintext history or a silently decoded wrong key is `FAIL`. | Public protection observation digest and independent database byte scan. | CONTRACT / 0.4 | Does not prove encryption strength or key management. |
| `session.conversation.scope-isolation` | Session Companion | `session-conversation`; `SessionScope`, `SessionStore` | Cross-scope access fails closed without side effects; cross-scope visibility or mutation is `FAIL`. | Public isolation observation digest and independent scope-row scan. | CONTRACT / 0.4 | Does not claim authorization or multitenant isolation. |
| `session.conversation.mutation` | Testing | `session-conversation`; `reconcile_session_recovery`, `reconcile_session_protection`, `ScenarioEvidenceBundle` | Controlled tamper, wrong-scope, wrong-key, and duplicate-submit mutations are all detected; an undetected mutation is a Harness error. | Runtime reconciliation result and independent mutation digest. | CONTRACT / 0.4 | Does not prove external ledger integrity. |
| `session.conversation.host-wheel` | Session Companion | Clean external venv; `python -I -m m_agent.testing` | The installed wheel runs the cross-store crash windows and the InMemory/SQLite store contract kit in one isolated process; source import or artifact identity mismatch is `FAIL`. | Installed-wheel probe observation digest and independent probe stdout digest. | HOST / 0.4 | Does not prove live provider or remote session store. |
| `context.compression.plan-order` | Runtime | `context-budget-compression`; `Runner.start_run`, `Runner.inspect_run` | The frozen Context Plan runs its RUN_INPUT stage first, the explicit `CONTEXT_COMPRESSION` model step in the middle, and the business model step last; a MODEL_STEP-scope stage triggered for compression is `FAIL`. | Public checkpoint order digest and independent sentinel digest. | CONTRACT / 0.4 | Does not prove nested compression. |
| `context.compression.frame-checkpoints` | Runtime | `context-budget-compression`; `Runner.inspect_run`, `CompressionResult` | Stage checkpoints preserve original items and derived items carry contract provenance; compression recomputed or an external source reread after recovery is `FAIL`. | Public checkpoint/provenance digest and independent sentinel digest. | CONTRACT / 0.4 | Does not claim lossless transform. |
| `context.compression.hard-budget` | Runtime | `context-budget-compression`; `check_frame_budget`, `ModelInputSizer` | An over-limit frame fails closed with zero business dispatch, and the public full-sizing recomputation agrees with the runtime verdict; an under-counting sizer admitting an over-limit frame is `FAIL`. | Public budget observation digest and independent sentinel digest. | CONTRACT / 0.4 | Does not claim soft budget or best-effort compression. |
| `context.compression.protected-channels` | Runtime | `context-budget-compression`; `Runner.start_run`, `CompressionContract` | Protected sources, conversation history, run input, and instructions never enter the compression request; canaries leaking into the compression input is `FAIL`. | Public channel observation digest and independent sentinel digest. | CONTRACT / 0.4 | Does not claim all context channels are compressible. |
| `context.compression.no-recursion` | Runtime | `context-budget-compression`; `Runner.start_run`, `ModelPurpose.CONTEXT_COMPRESSION` | Compression tool calls fail closed with zero business dispatch and compression never triggers the pipeline, tools, or repair; recursion is `FAIL`. | Public recursion observation digest and independent sentinel digest. | CONTRACT / 0.4 | Does not claim compression is a business step. |
| `context.compression.mutation` | Runtime | `context-budget-compression`; `reconcile_compression_observation` | Controlled stale-source, tampered-provenance, under-counting-sizer, and recursion mutations are all detected; a silently accepted mutation is a Harness error. | Runtime reconciliation result and independent mutation digest. | CONTRACT / 0.4 | Does not claim single-source verification. |
| `context.compression.host-wheel` | Runtime | Clean external venv; `python -I -m m_agent.testing` | The installed wheel runs the budget compression and recovery probes in one isolated process; source import or artifact identity mismatch is `FAIL`. | Installed-wheel probe observation digest and independent probe stdout digest. | HOST / 0.4 | Does not prove live provider or business context quality. |
| `model.routing.typed-capability` | Routing Companion | `model-routing`; `m_agent.companion.routing.ModelRouter`, `m_agent.runtime.ModelRequirements` | Typed capability and contract limits filter candidates with inspectable reasons; a capability or limit mismatch silently admitted is `FAIL`. | Routing authoritative probe digest and independent digest. | CONTRACT / 0.5 | Does not prove live provider capability behavior. |
| `model.routing.operational-limits` | Routing Companion | `model-routing`; `m_agent.companion.routing.OperationalLimitsGate`, `OperationalLimitsSnapshot` | Limits floor, unknown, missing, stale, and integrity paths are all resolved with stable reason codes; unknown limits treated as sufficient or stale as healthy is `FAIL`. | Routing authoritative probe digest and independent digest. | CONTRACT / 0.5 | Does not prove live quota probing or enforcement. |
| `model.routing.usage-cost` | Routing Companion | `model-routing`; `m_agent.companion.routing.estimate_run_cost`, `RunCostPolicy` | Declared formula, usage provenance, and gaps are reported without fabricated precision; a fabricated estimate or settlement guarantee claim is `FAIL`. | Routing authoritative probe digest and independent digest. | CONTRACT / 0.5 | Does not claim billing settlement or provider invoice accuracy. |
| `model.routing.deployment-constraints` | Routing Companion | `model-routing`; `m_agent.companion.routing.DeploymentConstraints`, `ModelCatalogEntry` | Provider, region, endpoint, and retention constraints match hard with unknown attributes failing closed; an unknown or disallowed attribute admitted is `FAIL`. | Routing authoritative probe digest and independent digest. | CONTRACT / 0.5 | Does not prove credential or sensitive endpoint configuration. |
| `model.routing.six-outcomes` | Routing Companion | `model-routing`; `m_agent.companion.routing.ModelRouter.select` | All six resolved outcomes are observed with inspectable reason codes; an unresolved or silent outcome is `FAIL`. | Routing authoritative probe digest and independent digest. | CONTRACT / 0.5 | Does not claim probabilistic or learning-based routing. |
| `model.routing.fallback` | Routing Companion | `model-routing`; `m_agent.companion.routing.execute_pre_run_fallback`, `FallbackSequence` | A frozen sequence with bounded attempts and inspectable reasons resolves before any Run creation; an unbounded, unregistered, or duplicate-identity sequence is `FAIL`. | Routing authoritative probe digest and independent digest. | CONTRACT / 0.5 | Does not claim in-run model switching. |
| `model.routing.zero-side-effect` | Routing Companion | `model-routing`; `m_agent.companion.routing.ModelRouter.select` | Success and failure paths leave the evidence catalog and policy digests unchanged; a mutated input snapshot or hidden dispatch is `FAIL`. | Routing authoritative probe digest and independent digest. | CONTRACT / 0.5 | Does not prove telemetry or diagnostic side channels. |
| `model.routing.immutable-decision` | Routing Companion | `model-routing`; `m_agent.companion.routing.SQLiteRoutingStore`, `bind_decision_to_run` | Deterministic decision identity supports idempotent replay, conflict failure, and reopen without recomputation; rewritten history or a recomputed decision on recovery is `FAIL`. | Routing authoritative probe digest and independent digest. | CONTRACT / 0.5 | Does not prove distributed or cross-process store contention. |
| `model.routing.no-in-run-switch` | Routing Companion | `model-routing`; `m_agent.companion.routing.register_replacement_run`, `RoutingReplacementError` | A running predecessor is never replaced and a successor requires a new decision; an in-run variant switch or decision reuse masquerading as retry is `FAIL`. | Routing authoritative probe digest and independent digest. | CONTRACT / 0.5 | Does not claim automatic failure recovery or retry policy. |
| `model.routing.explicit-promotion` | Routing Companion | `model-routing`; `m_agent.companion.routing.publish_recommendation_as_policy`, `register_variant_for_policy` | Publication preconditions fail closed and a published policy only affects future routing; an unpublished recommendation visible or a rewritten base policy is `FAIL`. | Routing authoritative probe digest and independent digest. | CONTRACT / 0.5 | Does not claim automatic promotion or baseline rerun. |
| `model.routing.mutation` | Testing | `model-routing`; `m_agent.testing.reconcile_model_routing`, `ScenarioEvidenceBundle` | Every flipped scenario observation boolean is detected by reconciliation; an undetected mutation is a Harness error. | Runtime reconciliation result and independent mutation digest. | CONTRACT / 0.5 | Does not prove external ledger integrity. |
| `model.routing.host-wheel` | Testing | Clean external venv; `python -I -m m_agent.testing` | The installed wheel runs every model routing contract probe in one isolated process; source import or artifact identity mismatch is `FAIL`. | Installed-wheel probe observation digest and independent probe stdout digest. | HOST / 0.5 | Does not prove live provider routing or quota enforcement. |
| `eval.regression.durable-recovery` | Eval Companion | `eval-regression`; `EvalExecutionEngine.run_suite`, `resume_execution`, `SQLiteEvalStore` | A crash resume completes the remaining items without rerunning completed units; duplicate execution or rerun of completed units is `FAIL`. | Eval authoritative probe digest and independent SQLite digest. | CONTRACT / 0.5 | Does not prove live provider or production eval store. |
| `eval.regression.judge-isolation` | Eval Companion | `eval-regression`; `EvalExecutionEngine`, `JudgeRunExecutor` | The judge uses a dedicated run store and results are append-only and reused; a judge sharing the subject store or rerunning is `FAIL`. | Eval authoritative probe digest and independent SQLite digest. | CONTRACT / 0.5 | Does not prove judge quality or live model behavior. |
| `eval.regression.baseline-comparison` | Eval Companion | `eval-regression`; `compare_report_revisions`, `BaselineComparison` | The five comparison states (unchanged, changed, new, missing, inconclusive) are verified; insufficient evidence misclassified as comparable is `FAIL`. | Eval authoritative probe digest and independent SQLite digest. | CONTRACT / 0.5 | Does not claim automatic baseline update or live provider drift. |
| `eval.regression.regression-detection` | Eval Companion | `eval-regression`; `compare_report_revisions`, `ComparisonOverall` | The hard gate detects pass-to-fail regressions and the quality gate follows policy; a missed regression or a no-change misclassified as regression is `FAIL`. | Eval authoritative probe digest and independent SQLite digest. | CONTRACT / 0.5 | Does not claim subjective quality or external baseline source. |
| `eval.regression.report-metrics` | Eval Companion | `eval-regression`; `build_report_revision`, `CaseVariantReport`, `summarize_samples` | Repetitions are retained with pass-at-k and justified statistics; an unjustified p95 or a swallowed failure sample is `FAIL`. | Eval authoritative probe digest and independent SQLite digest. | CONTRACT / 0.5 | Does not claim score calibration or cross-model comparison. |
| `eval.regression.observe-projection` | Eval Companion | `eval-regression`; `EvalObserver`, `ObservationSelection`, `project_observation` | Observe selection is read-only and projection is minimally authorized; an unauthorized field delivered or model dispatch during observe is `FAIL`. | Eval authoritative probe digest and independent SQLite digest. | CONTRACT / 0.5 | Does not claim sampling statistics or live provider observation. |
| `eval.regression.recommendation-readonly` | Eval Companion | `eval-regression`; `ModelRecommendationRecord`, `RecommendationTarget`, `SQLiteEvalStore` | A recommendation references frozen evidence without mutating stored facts; a stored fact mutation or a tampered recommendation accepted is `FAIL`. | Eval authoritative probe digest and independent SQLite digest. | CONTRACT / 0.5 | Does not claim automatic promotion or routing activation. |
| `eval.regression.mutation` | Testing | `eval-regression`; `m_agent.testing.reconcile_eval_regression`, `ScenarioEvidenceBundle` | Tampered report, baseline identity, and pass-at-k mutations are all detected; an undetected mutation is a Harness error. | Runtime reconciliation result and independent mutation digest. | CONTRACT / 0.5 | Does not prove external ledger integrity. |
| `eval.regression.host-wheel` | Testing | Clean external venv; `python -I -m m_agent.testing` | The installed wheel runs every eval regression contract probe in one isolated process; source import or artifact identity mismatch is `FAIL`. | Installed-wheel probe observation digest and independent probe stdout digest. | HOST / 0.5 | Does not prove live provider or production eval store. |

The 0.3 migration table is part of the `core.lifecycle.migration` contract. Its
complete 0.2 root-export mapping, including `Clock`, is kept in
[`migrating-to-0.3.md`](migrating-to-0.3.md).

## 0.5 platform matrix

The 0.5 release freezes the cross-platform requirement
(`FOUNDATION_PLATFORM_MATRIX_0_5`): Linux CPython 3.11, 3.12, 3.13, and 3.14
carry required CONTRACT evidence; Linux 3.11 is the primary HOST platform
where the full six-Scenario release profile runs against the installed wheel;
macOS (darwin) 3.11 and 3.14 carry secondary HOST evidence. Windows is not a
supported platform and is not declared. A platform cell that a release attempt
cannot cover is recorded as an honest `NOT_RUN` gap with its evidence source
— never silently satisfied, and never filled with another platform's or
another release candidate's evidence (all matrix observations bind the same
RC artifact digest). A matrix with gaps is `INCOMPLETE`, an observed failure
is `FAILED`, and only a fully covered matrix is `PASS`.

## 0.5 non-goals

The 0.5 candidate deliberately does not ship automatic promotion: a routing
recommendation is frozen read-only evidence, and publishing it as a policy is
an explicit, precondition-checked operator action that only affects future
routing. It does not ship in-run model switching: a running predecessor is
never replaced, a successor requires a new routing decision, and fallback
resolves entirely before any Run is created. It does not ship live provider
claims: CONTRACT and HOST evidence never establishes live provider behavior,
and PROVIDER-level qualification remains a separate, credential-gated,
opt-in verification. It does not claim production capacity, throughput, or
SLO evidence — the SQLite benchmarks are environment-qualified comparative
local evidence only. It does not claim exactly-once external effects; durable
recovery proves no duplicate execution after a crash resume, while external
side effects remain the application's resolution decision. 0.6 capabilities
(model catalog governance, automatic policy lifecycle, multi-candidate
routing) are out of scope for this candidate.

## 0.4 non-goals

The 0.4 candidate deliberately does not ship long-term memory: session history
is per-session, versioned, and protected at rest, but nothing is retained or
recalled across sessions. It does not ship history compression: conversation
history and other protected channels are passed through verbatim, and
compression applies only to explicitly contracted context items. It does not
ship a remote store: `SessionStore` and `RunStore` ship the InMemory and
SQLite implementations only, and no network storage, replication, or
cross-process coordination contract is claimed. PROVIDER evidence is labeled
`VALID`, `STALE`, or `NOT_RUN` by Model Contract fingerprint and a 30-day
freshness rule; CONTRACT and HOST results are never presented as live or
FIELD conclusions.
