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

---

# Durable Session conversation benchmark

Run the benchmark from the repository root:

```bash
uv run python benchmarks/session_workload.py \
  --database benchmarks/results/session-local.sqlite \
  --run-database benchmarks/results/session-run-local.sqlite \
  --json-output benchmarks/results/session-local.json
```

The command refuses to overwrite existing databases. It launches 10
concurrent Session conversations, each with 10 sequential Turns (100 Turns
total), through the public `SessionRunner.submit` API. The deterministic
`SessionWorkloadModel` echoes the input as the final output; the adapter uses
no credentials, provider clients, network services, or external databases.
Session history is protected by the Session PayloadCodec boundary
(`PlaintextPayloadCodec` here: a documented development/test codec, mirroring
the Durable Run benchmark); it is configured independently from the RunStore
PayloadCodec.

Before calculating metrics, the command validates every Session through
`SessionStore.get_session` and `SessionStore.read_snapshot` and requires
`version == turn_count`, residual claim count of zero, no duplicate turn run
identity, expected turn inputs and outputs, expected `definition_id`, and
`SUCCEEDED` status for every Turn's Run via `RunStore.get_run`. It then closes
both stores, reopens fresh `SQLiteSessionStore` and `SQLiteRunStore` instances,
re-reads every snapshot, and confirms the reopened history digest matches the
measured digest and that claim state is clean. Finally it checks expected
SQLite row counts (`sessions`, `session_claims = 0`, `session_turns`), no
orphaned lifecycle records, no turns outside the workload scope, and
successful `integrity_check` for both databases. Validation failure exits
nonzero and emits no `metrics` object or apparently valid performance numbers.

The measured interval begins when an explicit barrier releases all 10
concurrent workers and ends after all concurrent `submit` calls complete.
Validation, snapshot inspection, reopen comparison, and SQLite validation are
excluded. Per-Turn persistence overhead is the summed wall time of
`claim_run` and `commit_turn` for each committed Turn (timed via the
`TimedSQLiteSessionStore` subclass). SQLite schema initialization occurs
before measurement; warmup is therefore reported as zero Turns. The report also
records Python, OS, CPU, concurrency, session and run database paths,
journal/synchronous settings, and the exact measurement definition.

Results are comparative local evidence only. They are not universal thresholds
or production QPS, latency, scalability, availability, or capacity claims.
