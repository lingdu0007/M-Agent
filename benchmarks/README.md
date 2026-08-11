# Durable Run SQLite benchmark

Run the benchmark from the repository root:

```bash
uv run python benchmarks/durable_run_sqlite.py \
  --database benchmarks/results/durable-run-local.sqlite \
  --json-output benchmarks/results/durable-run-local.json
```

The command refuses to overwrite an existing database. It launches exactly
100 concurrent, sessionless Agent Runs through the public `Runner.create_run`
and `Runner.start_run` API. The deterministic Model Adapter requests one
IDEMPOTENT fake effect and then returns a final response; neither adapter uses
credentials, provider clients, network services, or an external database.

Before calculating metrics, the command inspects every Run through
`Runner.inspect_run` and requires `SUCCEEDED`, the `MODEL -> TOOL -> MODEL`
Step trajectory, one successful Attempt and Checkpoint per Step, exactly one
fake side effect per Run, expected SQLite row counts, no orphan records, and
successful `integrity_check` / `foreign_key_check`. Validation failure exits
nonzero and emits no `metrics` object or apparently valid performance numbers.

The measured interval begins when an explicit barrier releases all 100 workers
and ends after all concurrent `create_run` + `start_run` calls complete.
Inspection and validation are excluded. Run P50/P95 use linear interpolation.
Per-Step persistence overhead is the summed wall time of `record_step`,
`record_attempt`, and `record_checkpoint` for each completed Step. SQLite schema
initialization occurs before measurement; warmup is therefore reported as zero
Runs. The report also records Python, OS, CPU, concurrency, database path,
journal/synchronous settings, and the exact measurement definition.

Results are comparative local evidence only. They are not universal thresholds
or production QPS, latency, scalability, availability, or capacity claims.
