# Acceptance Coverage Matrix

This matrix is the frozen contract index for the 0.3 Runtime Baseline. Its
release profile is `runtime-baseline-0-3` at Pack version `runtime-baseline-v1`
and it freezes both required Scenarios. Every row, including its
owner, public seam, positive/negative assertion, evidence, milestone, and
non-claim, is frozen in a passing `core-lifecycle` Manifest. The Testing CLI
rejects a Manifest that differs from this complete set; a row cannot be
omitted based on an execution result.

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

The 0.3 migration table is part of the `core.lifecycle.migration` contract. Its
complete 0.2 root-export mapping, including `Clock`, is kept in
[`migrating-to-0.3.md`](migrating-to-0.3.md).
