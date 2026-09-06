# Historical Run Store Fixtures

`run_store_v050.tar.gz` contains native synthetic SQLite databases produced on
2026-09-06 by the actual published wheel in an external Python 3.13 environment:

- Wheel: `m_agent-0.5.0-py3-none-any.whl`
- Wheel SHA256: `8c2592715e840f5d8da4ce239c663864d0c24a16fa05edcefef09071c4fb59a6`
- Release commit: `743651e5c74a4865f25a31dab68d188b5b0aed64`
- Archive SHA256: `ae763b69d0a06c444a2d4d4caa0f187c7cc5f6d8f9fa7a021320adc6c2e29d0c`

The worker uses public Definition, Runner, RunStore, Model and Provider APIs.
Faults hard-exit with code 17 during an external adapter call or immediately
after the Store confirms a checkpoint. No fixture database was edited with
SQL to manufacture the identity collision or its recovery state.

| Database | Definition mode | Run | Confirmed state / fault |
| --- | --- | --- | --- |
| `legacy.db` | legacy Provider | `old-a` | after Context checkpoint |
| `explicit.db` | all three explicit scopes | `old-a` | after first Context checkpoint |
| `compression.db` | compression | `old-a` | after compression checkpoint |
| `pending.db` | legacy Provider | `old-a` | during Provider, no checkpoint |
| `pending-compression.db` | compression | `old-a` | during compression, Context confirmed |
| `corrupt.db` | legacy Provider | `old-a`, `old-b` | copy of legacy.db; second Run hits duplicate checkpoint |
| `compression-collision.db` | compression | `old-a`, `old-b` | copy of compression.db; distinct Provider stages expose the compression collision |

To reproduce the scenarios, install the verified wheel into a fresh external
environment, set `PYTHONPATH` to this repository's `tests` directory only, and
run outside the source checkout:

```bash
"$OLD_PYTHON" -c 'from fixtures.run_isolation_worker import main; main()' \
  "$EMPTY_OUTPUT/legacy.db" legacy old-a start --crash after-context
```

Use the corresponding mode and `--crash after-context`,
`--crash after-compression`, `--crash during-provider` or
`--crash during-compression` for each healthy file. The legacy collision runs
`legacy old-b start` against a copy of `legacy.db`. The compression collision
runs `compression old-b start --stage-suffix=-b` against a copy of
`compression.db`; different Context stage identities ensure the observed
failure is specifically at `compression:shared-compression:1`.

Expected collision result: `sqlite3.IntegrityError: UNIQUE constraint failed:
step_checkpoints.step_id`. Both damaged databases retain evidence whose
Step was replaced by the other Run.

Random Attempt IDs, wall-clock record timestamps and SQLite file headers
mean a fresh reproduction need not match archive bytes. The checked-in
archive is immutable historical input; tests extract only named members
into temporary files and assert through public inspection and migration
outcomes. No test downloads a wheel or makes a provider request.
