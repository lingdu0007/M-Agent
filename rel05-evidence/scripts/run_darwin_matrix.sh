set -e
echo "=== darwin platform matrix runner (Ticket 22) ==="
RC_WHEEL=/Users/lingdu/workspace/agent/hello-agent/dist/m_agent-0.5.0-py3-none-any.whl
RC_SDIST=/Users/lingdu/workspace/agent/hello-agent/dist/m_agent-0.5.0.tar.gz
RC_DIGEST="sha256:$(shasum -a 256 "$RC_WHEEL" | cut -d' ' -f1)"
echo "RC_DIGEST=$RC_DIGEST"
BASE=/Users/lingdu/rel05-matrix

for v in 311 314; do
  MINOR="${v:0:1}.${v:1:2}"
  echo "=== darwin $MINOR: venv + install ==="
  rm -rf "$BASE/venv-darwin-$v"
  uv venv --python "$MINOR" "$BASE/venv-darwin-$v"
  uv pip install --python "$BASE/venv-darwin-$v/bin/python" "m-agent[testing] @ file://$RC_WHEEL"
  mkdir -p "$BASE/darwin-$MINOR"
  echo "=== darwin $MINOR: manifest ==="
  "$BASE/venv-darwin-$v/bin/python" "$BASE/make_manifest.py" "$RC_WHEEL" "$RC_SDIST" "$BASE/darwin-$MINOR/manifest.json"
  echo "=== darwin $MINOR: release pack ==="
  "$BASE/venv-darwin-$v/bin/python" -m m_agent.testing run \
    --manifest "$BASE/darwin-$MINOR/manifest.json" \
    --wheel "$RC_WHEEL" --sdist "$RC_SDIST" \
    --output-dir "$BASE/darwin-$MINOR/output"
  echo "=== darwin $MINOR: pack exit=$? ==="
  echo "=== darwin $MINOR: observations ==="
  "$BASE/venv-darwin-$v/bin/python" "$BASE/extract_observations.py" \
    "$BASE/darwin-$MINOR/output" darwin "$MINOR" "$RC_DIGEST" \
    "host macOS darwin/arm64 CPython $MINOR isolated venv: foundation-release-0-5 pack execution (six scenario bundles)" \
    "$BASE/darwin-$MINOR/observations.json" "HOST:secondary"
done
echo "=== darwin matrix runner complete ==="
