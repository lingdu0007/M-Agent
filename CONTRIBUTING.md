# Contributing to M-Agent

M-Agent is an embeddable Agent Application Runtime for Python. Keep changes
inside the runtime boundary: do not add a hosted control plane, scheduler,
worker fleet, UI, Session/RAG/Workflow/MultiAgent core, or provider credential
handling.

Before opening a change:

1. Read `CONTEXT.md`, relevant ADRs, and the owning `.scratch` issue.
2. Add behavior tests through public Runner and RunStore interfaces.
3. Run focused tests, the complete offline suite, `compileall`, and
   `git diff --check`.
4. Keep deterministic fakes, credential-gated live evidence, and benchmark
   results clearly separate.

Contributions are licensed under Apache-2.0. Do not include credentials,
private endpoints, raw provider payloads, absolute local paths, or personal
configuration in commits. Report security issues through `SECURITY.md` rather
than a public issue.
