# Acceptance Coverage Matrix

This matrix is the frozen contract index for the Ticket 07
`core-lifecycle` Foundation Pack. Its only profile is
`core-lifecycle-foundation` at Pack version `foundation-v1`; it is not the
later six-scenario `foundation-release` profile. Every row, including its
owner, public seam, positive/negative assertion, evidence, milestone, and
non-claim, is frozen in a passing `core-lifecycle` Manifest. The Testing CLI
rejects a Manifest that differs from this complete set; a row cannot be
omitted based on an execution result.

| Check ID | Owner | Scenario and public seam | Positive and negative check | Authority and independent evidence | Level / milestone | Non-claim |
| --- | --- | --- | --- | --- | --- | --- |
| `core.lifecycle` | Runtime Core | `core-lifecycle`; `m_agent.runtime.Runner` | Create, start, inspect one deterministic Run; a non-success terminal is `FAIL`. | Public `RunInspection` counts, packaged fixture digest, and isolated wheel-process observation. | CONTRACT / Foundation | Does not prove provider or production execution. |
| `core.lifecycle.unknown-definition` | Runtime Core | `core-lifecycle`; `m_agent.runtime.DefinitionRegistry` | An unknown Definition raises the public error; successful resolution is `FAIL`. | Public Registry result and isolated wheel-process observation digest. | CONTRACT / Foundation | Does not prove Definition persistence. |
| `core.lifecycle.public-namespaces` | Distribution API | `core-lifecycle`; `m_agent.runtime`, `m_agent.adapters`, `m_agent.companion`, `m_agent.testing` | All four public namespaces import; a missing or wrong public binding is `FAIL`. | Installed wheel import view and isolated wheel-process observation digest. | CONTRACT / Foundation | Does not prove future Companion capabilities. |
| `core.lifecycle.dependency-direction` | Runtime Core | `core-lifecycle`; `m_agent.testing.find_runtime_dependency_violations` | The installed Core has no reverse layer import; a forbidden import is `FAIL`. | Static installed-Core source scan and isolated wheel-process observation digest. | CONTRACT / Foundation | Does not prove dynamic behavior outside public Core files. |
| `core.lifecycle.expand-compatibility` | Distribution API | `core-lifecycle`; `m_agent`, `m_agent.runtime` | Root `Runner` and `Clock` retain their semantic Runtime bindings; a changed binding is `FAIL`. | Public import identity and isolated wheel-process observation digest. | CONTRACT / 0.2 expand | Does not claim 0.3 root retention. |
| `core.lifecycle.telemetry` | Runtime Adapter | `core-lifecycle`; `m_agent.adapters.JsonlTelemetrySink` | Closed JSONL events correlate to the public Run and lifecycle; a payload-bearing or uncorrelated event is `FAIL`. | Public event-contract digest and isolated wheel-process observation digest. | CONTRACT / Foundation | Does not claim external Collector behavior or replace Store authority. |
| `core.lifecycle.bundle-tamper` | Testing | `core-lifecycle`; `m_agent.testing.ScenarioEvidenceBundle` | A controlled changed Bundle must fail integrity verification; an undetected mutation is a Harness error. | Content digest and independently measured fixture digest. | CONTRACT / Foundation | Does not prove external ledger integrity. |
| `core.lifecycle.host-wheel` | Testing | Clean external venv; `python -I -m m_agent.testing` | Exact built wheel and sdist install without editable mode, source path injection, or private imports; changed source/artifact/fixture/environment identity is rejected. | Wheel and sdist SHA-256, all installed wheel members, packaged fixture bytes, build-tool version, Testing dependency summary, and isolated SQLite restart observation. | HOST / Foundation | Does not prove live provider behavior. |

The 0.3 migration table is part of the `core.lifecycle.expand-compatibility`
contract. Its complete 0.2 root-export mapping, including `Clock`, is kept in
[`migrating-to-0.3.md`](migrating-to-0.3.md).
