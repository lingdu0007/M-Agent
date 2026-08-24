# M-Agent

M-Agent 0.3.0 is an embeddable Agent Application Runtime for Python. Install
the `m-agent` distribution and import `m_agent`.

```bash
python -m pip install "m-agent[provider]"
```

The core data contract uses Pydantic. The focused `provider` extra installs
`httpx`; `storage` and `telemetry` select the shipped standard-library SQLite
and JSONL integrations without adding dependencies; `security` names the
application-supplied `PayloadCodec` boundary. The optional local
`OpenTelemetryTelemetrySink` maps the public redacted Telemetry contract to a
caller-owned exporter; it does not claim Collector, provider, or production
observability verification.

## Runtime Foundation

The 0.3 release is a deliberate public contract reset. The root package keeps
only the high-frequency Run facade; removed 0.2 imports fail with directional
migration errors. New integrations use `m_agent.runtime` for Core contracts and
ports, `m_agent.adapters` for concrete implementations,
`m_agent.companion` for optional composition capabilities, and
`m_agent.testing` for the offline Acceptance Pack. Core does not import the
other three layers. See [the 0.3 migration table](docs/migrating-to-0.3.md).

## Durable Run

`Runner` is async-first and executes one explicit Agent Run. The embedding
application owns workers, queues, scheduling, credentials, definition
registration, and resolution decisions. See [the Durable Run guide](docs/durable-run.md).

For scripts and synchronous applications, `SyncRunner` delegates every command
to the same async state machine:

```python
from m_agent import (
    AgentDefinition,
    DefinitionRegistry,
    Runner,
    SyncRunner,
)
from m_agent.adapters import (
    DeterministicModelAdapter,
    InMemoryRunStore,
    PlaintextPayloadCodec,
)

registry = DefinitionRegistry()
registry.register(AgentDefinition.for_adapter(
    definition_id="hello",
    version="1.0",
    instructions="Answer deterministically.",
    model_adapter=DeterministicModelAdapter(responses=("hello",)),
))

store = InMemoryRunStore(payload_codec=PlaintextPayloadCodec())
with SyncRunner(Runner(registry=registry, store=store)) as runner:
    created = runner.create_run("hello", "1.0", "hi")
    terminal = runner.start_run(created.run_id)
    print(terminal.status, terminal.output)
```

The deterministic flagship is `examples/durable_support_agent`. It shows crash
recovery, an uncertain non-idempotent notification, `WAITING`, and explicit
application resolution without credentials or network access:

```bash
python examples/durable_support_agent/run_acceptance.py
```

## Evidence Boundaries

- The deterministic fake adapters and flagship example are offline demonstrations; they do not establish provider compatibility.
- Live Chat Completions and Responses contracts are credential-gated and opt-in; see [live adapter evidence](docs/live-model-adapter-contracts.md).
- The 100-Run SQLite result is environment-qualified comparative evidence, not a universal production QPS, latency, availability, or capacity claim; it is not production capacity evidence. See [benchmark methodology](benchmarks/README.md).

## Python and Migration

Supported Python is 3.11 or newer. CONTRACT coverage targets Linux Python
3.11-3.14; HOST evidence is Linux Python 3.11. macOS and Windows gaps are
recorded in the Coverage Matrix, and Windows is not supported. The 0.1
`agent_framework` path is removed in 0.3.0; see [the migration table](docs/migrating-to-0.3.md).

## Project Material

- [Apache-2.0 license](LICENSE)
- [Contribution guide](CONTRIBUTING.md)
- [Private security reporting](SECURITY.md)
- [Durable Run documentation](docs/durable-run.md)
- [Live adapter documentation](docs/live-model-adapter-contracts.md)
