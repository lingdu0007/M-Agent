set -e
echo "=== Linux platform matrix runner (Ticket 22) ==="
uv --version || true

# 1. newer uv knows CPython 3.14; pip installs console script over /usr/local/bin/uv
python -m pip install --quiet uv 2>&1 | grep -v notice || true
echo "uv after upgrade: $(uv --version)"

# 2. install Linux CPython 3.12/3.13/3.14 (3.11.11 is the image system python)
uv python install 3.12 3.13 3.14 2>&1 | tail -3

# 3. pre-warm uv build cache with the exact setuptools 84.0.0 used for the RC
mkdir -p /tmp/prewarm
cd /tmp/prewarm
tar -xzf /rc-dist/m_agent-0.5.0.tar.gz
echo "setuptools==84.0.0" > /tmp/constraints.txt
cd m_agent-0.5.0
uv build --wheel --out-dir /tmp/prewarm-wheel --build-constraint /tmp/constraints.txt 2>&1 | tail -2

RC_WHEEL=/rc-dist/m_agent-0.5.0-py3-none-any.whl
RC_SDIST=/rc-dist/m_agent-0.5.0.tar.gz
RC_DIGEST="sha256:$(sha256sum "$RC_WHEEL" | cut -d' ' -f1)"
echo "RC_DIGEST=$RC_DIGEST"

# 4. run the full 0.5 release profile for every Linux python
for v in 3.11 3.12 3.13 3.14; do
  echo "=== Linux $v: venv + install ==="
  rm -rf /tmp/venv-$v
  uv venv --python $v /tmp/venv-$v
  uv pip install --python /tmp/venv-$v/bin/python "m-agent[testing] @ file://$RC_WHEEL"
  mkdir -p /matrix/linux-$v
  echo "=== Linux $v: manifest ==="
  /tmp/venv-$v/bin/python /matrix/make_manifest.py "$RC_WHEEL" "$RC_SDIST" "/matrix/linux-$v/manifest.json"
  echo "=== Linux $v: release pack ==="
  /tmp/venv-$v/bin/python -m m_agent.testing run \
    --manifest "/matrix/linux-$v/manifest.json" \
    --wheel "$RC_WHEEL" --sdist "$RC_SDIST" \
    --output-dir "/matrix/linux-$v/output"
  echo "=== Linux $v: pack exit=$? ==="
  if [ "$v" = "3.11" ]; then
    CELLS="CONTRACT:required,HOST:primary"
  else
    CELLS="CONTRACT:required"
  fi
  echo "=== Linux $v: observations ==="
  /tmp/venv-$v/bin/python /matrix/extract_observations.py \
    "/matrix/linux-$v/output" linux "$v" "$RC_DIGEST" \
    "Docker linux/arm64 bookworm CPython $v isolated venv: foundation-release-0-5 pack execution (six scenario bundles)" \
    "/matrix/linux-$v/observations.json" "$CELLS"
done
echo "=== Linux matrix runner complete ==="
