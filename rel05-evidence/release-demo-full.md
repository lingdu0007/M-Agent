# m-agent 0.5 Runtime Foundation — Release Demo (12–15 minutes)

PRE-GENERATED EVIDENCE: every observation in this demo is replayed from previously verified Scenario Evidence Bundles for the exact release candidate below. Nothing is executed live in this room.

Evidence levels shown are CONTRACT (offline harness) and HOST (installed wheel). PROVIDER-level live-provider evidence is out of scope for this replay — this demo is NOT a live provider demonstration and makes no live, production capacity, or SLO claim.

Release candidate (one Manifest identity binds every Scenario):
- distribution: m-agent 0.5.0
- artifact digest: sha256:4cf648474e6461e0285459a00d4f43ca3f26eff88ffe4c35fdece630bf74cb27
- sdist digest: sha256:09d458160b66509a4e0f393e41e6eafb0f235cab992689bb23189492bdb97263
- fixture digest: sha256:76d7fee8fbe3293841daf42091e723ab14e5c974121a23f66dfc21ee8b56562d
- source commit: ed7177d047a528070c23bc06c84fb10c7817d2fb
- profile: foundation-release-0-5 (pack foundation-release-0-5-v1)
- manifest digest: sha256:560156e5ca26cc706c31c7231fbe7329ba56c3f800fd6fc1f0700758a6e6f2be

## Run order

1. core-lifecycle (2 min)
2. durable-effects-recovery (2 min)
3. session-conversation (2 min)
4. context-budget-compression (2 min)
5. model-routing (2 min)
6. eval-regression (2 min)

## core-lifecycle (2 min)

PRE-GENERATED EVIDENCE — replayed from verified Bundle sha256:2b68cbfc59b22a376122b33f251fa2556112c3495146502c1f5aacdb3866a3cb

Replayed checks:
- `core.lifecycle`: PASS — CONTRACT evidence via `core_lifecycle_authoritative_digest`; non-claim: provider_or_production_execution
- `core.lifecycle.telemetry`: PASS — CONTRACT evidence via `telemetry_authoritative_digest`; non-claim: provider_endpoint_or_production_observability
- `core.lifecycle.public-namespaces`: PASS — CONTRACT evidence via `public_namespaces_authoritative_digest`; non-claim: future_companion_capabilities
- `core.lifecycle.dependency-direction`: PASS — CONTRACT evidence via `dependency_direction_authoritative_digest`; non-claim: dynamic_behavior_outside_core_files
- `core.lifecycle.migration`: PASS — CONTRACT evidence via `migration_authoritative_digest`; non-claim: backward_compatible_0_2_public_surface
- `core.lifecycle.unknown-definition`: PASS — CONTRACT evidence via `unknown_definition_authoritative_digest`; non-claim: definition_persistence
- `core.lifecycle.host-wheel`: PASS — HOST evidence via `host_wheel_authoritative_digest`; non-claim: live_provider_behavior
- `core.lifecycle.telemetry-host`: PASS — HOST evidence via `telemetry_host_authoritative_digest`; non-claim: external_collector_or_live_provider_behavior
- `core.lifecycle.bundle-tamper`: PASS — CONTRACT evidence via `bundle_tamper_authoritative_digest`; non-claim: external_ledger_integrity

## durable-effects-recovery (2 min)

PRE-GENERATED EVIDENCE — replayed from verified Bundle sha256:349883df9026d01fb35c1cc8ce246c4083c6b726fcb76b96a48719fed32c7b4d

Replayed checks:
- `durable.effects.recovery-windows`: PASS — CONTRACT evidence via `recovery_windows_authoritative_digest`; non-claim: exactly_once_external_effect
- `durable.effects.budget-fail-closed`: PASS — CONTRACT evidence via `budget_fail_closed_authoritative_digest`; non-claim: provider_quota_or_cost_budget
- `durable.effects.waiting-resolution`: PASS — CONTRACT evidence via `waiting_resolution_authoritative_digest`; non-claim: automatic_uncertain_effect_resolution
- `durable.effects.mutation`: PASS — CONTRACT evidence via `mutation_authoritative_digest`; non-claim: business_effect_evidence
- `durable.effects.host-wheel`: PASS — HOST evidence via `durable_host_authoritative_digest`; non-claim: live_provider_or_production_effect

## session-conversation (2 min)

PRE-GENERATED EVIDENCE — replayed from verified Bundle sha256:29c4aa5b9b9c036ee7cd64cd6e0d3c03c1fc48863f51eaf15d07253d7851ca25

Replayed checks:
- `session.conversation.recovery-windows`: PASS — CONTRACT evidence via `session_recovery_windows_authoritative_digest`; non-claim: exactly_once_external_effect
- `session.conversation.claim-no-ttl`: PASS — CONTRACT evidence via `session_claim_no_ttl_authoritative_digest`; non-claim: time_based_claim_recovery
- `session.conversation.payload-protection`: PASS — CONTRACT evidence via `session_payload_protection_authoritative_digest`; non-claim: encryption_strength_or_key_management
- `session.conversation.scope-isolation`: PASS — CONTRACT evidence via `session_scope_isolation_authoritative_digest`; non-claim: authorization_or_multitenant_isolation
- `session.conversation.mutation`: PASS — CONTRACT evidence via `session_mutation_authoritative_digest`; non-claim: external_ledger_integrity
- `session.conversation.host-wheel`: PASS — HOST evidence via `session_host_authoritative_digest`; non-claim: live_provider_or_remote_session_store

## context-budget-compression (2 min)

PRE-GENERATED EVIDENCE — replayed from verified Bundle sha256:74a7e9d4c735bcd9c8d44ba1bbf81bf42d7a52d97678c800c2905fae976fe17b

Replayed checks:
- `context.compression.plan-order`: PASS — CONTRACT evidence via `context_plan_order_authoritative_digest`; non-claim: nested_compression
- `context.compression.frame-checkpoints`: PASS — CONTRACT evidence via `context_frame_checkpoints_authoritative_digest`; non-claim: lossless_transform
- `context.compression.hard-budget`: PASS — CONTRACT evidence via `context_hard_budget_authoritative_digest`; non-claim: soft_budget_or_best_effort_compression
- `context.compression.protected-channels`: PASS — CONTRACT evidence via `context_protected_channels_authoritative_digest`; non-claim: all_context_channels_are_compressible
- `context.compression.no-recursion`: PASS — CONTRACT evidence via `context_no_recursion_authoritative_digest`; non-claim: compression_is_a_business_step
- `context.compression.mutation`: PASS — CONTRACT evidence via `context_mutation_authoritative_digest`; non-claim: single_source_verification
- `context.compression.host-wheel`: PASS — HOST evidence via `context_host_authoritative_digest`; non-claim: live_provider_or_business_context_quality

## model-routing (2 min)

PRE-GENERATED EVIDENCE — replayed from verified Bundle sha256:0f606ee6d4f99f6687b6e0f119728d91c57dda3932743a7a0c75ec90397e6480

Replayed checks:
- `model.routing.typed-capability`: PASS — CONTRACT evidence via `routing_typed_capability_authoritative_digest`; non-claim: live_provider_capability_behavior
- `model.routing.operational-limits`: PASS — CONTRACT evidence via `routing_operational_limits_authoritative_digest`; non-claim: live_quota_probing_or_enforcement
- `model.routing.usage-cost`: PASS — CONTRACT evidence via `routing_usage_cost_authoritative_digest`; non-claim: billing_settlement_or_provider_invoice_accuracy
- `model.routing.deployment-constraints`: PASS — CONTRACT evidence via `routing_deployment_constraints_authoritative_digest`; non-claim: credential_or_sensitive_endpoint_configuration
- `model.routing.six-outcomes`: PASS — CONTRACT evidence via `routing_six_outcomes_authoritative_digest`; non-claim: probabilistic_or_learning_based_routing
- `model.routing.fallback`: PASS — CONTRACT evidence via `routing_fallback_authoritative_digest`; non-claim: in_run_model_switching
- `model.routing.zero-side-effect`: PASS — CONTRACT evidence via `routing_zero_side_effect_authoritative_digest`; non-claim: telemetry_or_diagnostic_side_channels
- `model.routing.immutable-decision`: PASS — CONTRACT evidence via `routing_immutable_decision_authoritative_digest`; non-claim: distributed_or_cross_process_store_contention
- `model.routing.no-in-run-switch`: PASS — CONTRACT evidence via `routing_no_in_run_switch_authoritative_digest`; non-claim: automatic_failure_recovery_or_retry_policy
- `model.routing.explicit-promotion`: PASS — CONTRACT evidence via `routing_explicit_promotion_authoritative_digest`; non-claim: automatic_promotion_or_baseline_rerun
- `model.routing.mutation`: PASS — CONTRACT evidence via `routing_mutation_authoritative_digest`; non-claim: external_ledger_integrity
- `model.routing.host-wheel`: PASS — HOST evidence via `routing_host_authoritative_digest`; non-claim: live_provider_routing_or_quota_enforcement

## eval-regression (2 min)

PRE-GENERATED EVIDENCE — replayed from verified Bundle sha256:ef6071288e2fbbc97e3f83aca24305d9088428d90a82856e2e79c9376d1e8c09

Replayed checks:
- `eval.regression.durable-recovery`: PASS — CONTRACT evidence via `eval_durable_recovery_authoritative_digest`; non-claim: live_provider_or_production_eval_store
- `eval.regression.judge-isolation`: PASS — CONTRACT evidence via `eval_judge_isolation_authoritative_digest`; non-claim: judge_quality_or_live_model_behavior
- `eval.regression.baseline-comparison`: PASS — CONTRACT evidence via `eval_baseline_comparison_authoritative_digest`; non-claim: automatic_baseline_update_or_live_provider_drift
- `eval.regression.regression-detection`: PASS — CONTRACT evidence via `eval_regression_detection_authoritative_digest`; non-claim: subjective_quality_or_external_baseline_source
- `eval.regression.report-metrics`: PASS — CONTRACT evidence via `eval_report_metrics_authoritative_digest`; non-claim: score_calibration_or_cross_model_comparison
- `eval.regression.observe-projection`: PASS — CONTRACT evidence via `eval_observe_projection_authoritative_digest`; non-claim: sampling_statistics_or_live_provider_observation
- `eval.regression.recommendation-readonly`: PASS — CONTRACT evidence via `eval_recommendation_authoritative_digest`; non-claim: automatic_promotion_or_routing_activation
- `eval.regression.mutation`: PASS — CONTRACT evidence via `eval_mutation_authoritative_digest`; non-claim: external_ledger_integrity
- `eval.regression.host-wheel`: PASS — HOST evidence via `eval_host_authoritative_digest`; non-claim: live_provider_or_production_eval_store

## Closing: what this demo does and does not claim

The Pack verdict replayed here is PASSED for this release candidate's six required Scenarios. This is offline acceptance evidence only: it is NOT a live provider run, it does not claim exactly-once external effects, automatic promotion, in-run model switching, or any 0.6 capability.
