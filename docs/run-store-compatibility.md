# SQLite Run Identity Compatibility

## Scope And Release Status

The shared-Run identity repair is a source change, not a published package
release. It does not change the package version or claim a new release.

The defect was reproduced against both source baseline
`309ca6a05f355f7c132a3d51ee02a02ed5292455` and the published
`m_agent-0.5.0-py3-none-any.whl`, SHA256
`8c2592715e840f5d8da4ce239c663864d0c24a16fa05edcefef09071c4fb59a6`,
from release commit `743651e5c74a4865f25a31dab68d188b5b0aed64`.
The different wheel digest archived in `rel05-evidence/` belongs to an earlier
RC; those archived results are not new verification of this repair.

## Identity Contract

An Agent Run owns its Step, Attempt, Checkpoint and payload records:

- Step and Checkpoint keys are `(run_id, step_id)`.
- Attempt keys are `(run_id, attempt_id)`.
- Encoded payload keys remain `(run_id, field)`.

These keys align SQLite with the existing InMemoryRunStore and public
Run-scoped Store queries. Callers must correlate IDs with their Run, including
when consuming telemetry. This is not a tenant authorization boundary.

The Runner's deterministic Context invocation IDs and compression IDs are
unchanged. The implicit Provider and explicit `RUN_INPUT`, `TOOL_OUTCOME` and
`MODEL_STEP` stages retain their existing stage/scope/boundary identities.
Compression retains its frozen contract identity. Same-Run recovery reuses
completed checkpoints; unconfirmed invocations retain the Step and create
a new Attempt under existing recovery and budget rules.

No Agent Definition, Definition version, frozen Snapshot, checkpoint payload
format or provenance needs to be rewritten. Pre-Context-Plan bare Context Item
checkpoints retain the Runner's existing compatibility path.

## Migration

The reference SQLite Store uses `PRAGMA user_version = 1` for the Run-scoped
identity schema. On opening a supported historical database it:

1. Acquires a SQLite write transaction, serializing concurrent openers.
2. Performs the existing metadata/payload compatibility migrations.
3. Validates historical Step/Attempt/Checkpoint ownership and required
   checkpoint payload presence before changing identity tables.
4. Rebuilds the three identity tables with composite primary keys, preserving
   row order, record identifiers, timestamps and contents. The existing
   `run_payloads` bytes are not decoded/re-encoded for this identity migration.
5. Records the schema version and commits all changes atomically.

Failure rolls back the entire opening transaction and closes the connection.
Migration does not reset a Run version, lease owner/expiry, retry history or
Model Execution Budget. Concurrent opens are supported; concurrent use of
an old binary during upgrade is not.

Before upgrade, stop all old writers and keep a consistent backup using the
application's normal SQLite backup procedure. Upgrade all processes that open
the file together. Do not downgrade or mix old and new Store implementations:
older binaries do not enforce the new schema-version contract. A rollback
requires a pre-upgrade backup and an explicit application recovery decision;
it cannot silently discard Runs written since that backup.

Unknown schema versions or primary-key layouts are refused. Custom columns,
indexes or triggers on a table requiring rebuild require an explicit
migration; the built-in migration does not silently remove them.

## Damaged Historical Records

In 0.5.0, a later Run could replace an earlier Run's Step before encountering
the checkpoint unique-key error. Compression has the same failure pattern.
The remaining Attempt/Checkpoint may therefore refer to a Step now owned by
another Run. A second Run need not have completed for damage to exist.

The migration raises the public `RunStoreIntegrityError`
(`code = RUN_STORE_INTEGRITY_ERROR`) for inconsistent historical ownership or
missing required evidence. It refuses the whole migration and preserves the
database. It does not infer a missing Step from output, use another Run's
Attempt, rerun a completed Provider, omit corrupt records or pretend that the
historical execution completed successfully.

Retain the original file and investigate a backup or a separate read-only
forensic copy. Repair from independent authoritative evidence, if available,
is an explicit operator responsibility. The runtime cannot reconstruct
overwritten history or prove the absence of damage that left no surviving
evidence. Renaming IDs, modifying a frozen Definition, deleting the database
or assigning one database per Run is not this repair.

## Verification Boundaries

`tests/test_m_agent_run_isolation.py` covers the real Runner/SQLite boundary,
independent connections, deliberate interleaving, all Context scopes,
compression, and three repetitions of each process-crash window.
It compares original inspections after other Runs execute and uses an
independent dispatch journal for completed-work reuse.

`tests/test_m_agent_store_migration.py` loads databases generated through the
published wheel's public APIs, including two actual collision-damaged files.
It checks recovery, byte preservation, concurrent opens, idempotence and
rollback on unsupported schema. The shared Store contract also exercises
equal Step and Attempt IDs with distinct Run payloads.

These are synthetic CONTRACT/local HOST checks, not live-provider, production
capacity, authorization, exactly-once or cross-platform release certification.
