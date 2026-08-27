set -eu
cd /Users/lingdu/workspace/agent/hello-agent
PY=/Users/lingdu/rel05-matrix/venv-darwin-311/bin/python
export M_AGENT_BENCHMARK_ARTIFACT_DIGEST="sha256:4cf648474e6461e0285459a00d4f43ca3f26eff88ffe4c35fdece630bf74cb27"
export M_AGENT_BENCHMARK_MANIFEST_DIGEST="sha256:560156e5ca26cc706c31c7231fbe7329ba56c3f800fd6fc1f0700758a6e6f2be"
R=benchmarks/results

echo "=== benchmark 1/4: durable_run_sqlite ==="
"$PY" benchmarks/durable_run_sqlite.py \
  --database "$R/rel05-durable-run-local.sqlite" \
  --json-output "$R/release-0.5-durable-run-python311-macos-arm64.json"

echo "=== benchmark 2/4: session_workload ==="
"$PY" benchmarks/session_workload.py \
  --database "$R/rel05-session-local.sqlite" \
  --run-database "$R/rel05-session-run-local.sqlite" \
  --json-output "$R/release-0.5-session-python311-macos-arm64.json"

echo "=== benchmark 3/4: context_workload ==="
"$PY" benchmarks/context_workload.py \
  --database "$R/rel05-context-workload-local.sqlite" \
  --json-output "$R/release-0.5-context-python311-macos-arm64.json"

echo "=== benchmark 4/4: eval_workload ==="
"$PY" benchmarks/eval_workload.py \
  --database "$R/rel05-eval-workload-local.sqlite" \
  --json-output "$R/release-0.5-eval-python311-macos-arm64.json"

echo "=== all benchmarks complete ==="
