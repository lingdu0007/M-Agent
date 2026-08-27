# m-agent 0.5 Runtime Foundation — Recovery Demo (3 minutes)

PRE-GENERATED EVIDENCE: this short demo replays the crash-recovery story and the release report from verified Bundles. Nothing is executed live in this room.

PROVIDER-level live-provider evidence is out of scope for this replay — this demo is NOT a live provider demonstration.

Release candidate (one Manifest identity binds every Scenario):
- distribution: m-agent 0.5.0
- artifact digest: sha256:4cf648474e6461e0285459a00d4f43ca3f26eff88ffe4c35fdece630bf74cb27
- sdist digest: sha256:09d458160b66509a4e0f393e41e6eafb0f235cab992689bb23189492bdb97263
- fixture digest: sha256:76d7fee8fbe3293841daf42091e723ab14e5c974121a23f66dfc21ee8b56562d
- source commit: ed7177d047a528070c23bc06c84fb10c7817d2fb
- profile: foundation-release-0-5 (pack foundation-release-0-5-v1)
- manifest digest: sha256:560156e5ca26cc706c31c7231fbe7329ba56c3f800fd6fc1f0700758a6e6f2be

## durable-effects-recovery (2 min)

PRE-GENERATED EVIDENCE — replayed from verified Bundle sha256:349883df9026d01fb35c1cc8ce246c4083c6b726fcb76b96a48719fed32c7b4d

Replayed checks:
- `durable.effects.recovery-windows`: PASS — CONTRACT evidence via `recovery_windows_authoritative_digest`; non-claim: exactly_once_external_effect
- `durable.effects.budget-fail-closed`: PASS — CONTRACT evidence via `budget_fail_closed_authoritative_digest`; non-claim: provider_quota_or_cost_budget
- `durable.effects.waiting-resolution`: PASS — CONTRACT evidence via `waiting_resolution_authoritative_digest`; non-claim: automatic_uncertain_effect_resolution
- `durable.effects.mutation`: PASS — CONTRACT evidence via `mutation_authoritative_digest`; non-claim: business_effect_evidence
- `durable.effects.host-wheel`: PASS — HOST evidence via `durable_host_authoritative_digest`; non-claim: live_provider_or_production_effect

## Report (1 min)

PRE-GENERATED EVIDENCE — the full Pack for this release candidate completed with status PASSED (exit 0); the complete 12–15 minute demo replays all six required Scenarios.
