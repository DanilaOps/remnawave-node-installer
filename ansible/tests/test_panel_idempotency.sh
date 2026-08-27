#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
state="$(mktemp)"
first="$(mktemp)"
second="$(mktemp)"
node_env="$(mktemp)"
rm -f "$state"
rm -f "$node_env"

cleanup() {
  kill "${server_pid:-}" 2>/dev/null || true
  rm -f "$state" "$first" "$second" "$node_env"
}
trap cleanup EXIT

python "$root/tests/mock_panel.py" --state "$state" &
server_pid=$!

for _ in $(seq 1 30); do
  curl --silent --fail http://127.0.0.1:18080/api/nodes >/dev/null && break
  sleep 0.1
done

ANSIBLE_CONFIG="$root/ansible.cfg" ansible-playbook \
  -i localhost, -c local "$root/tests/panel_idempotency.yml" \
  -e test_node_env_path="$node_env" | tee "$first"
ANSIBLE_CONFIG="$root/ansible.cfg" ansible-playbook \
  -i localhost, -c local "$root/tests/panel_idempotency.yml" \
  -e test_node_env_path="$node_env" | tee "$second"

grep -Eq 'changed=0([[:space:]]|$)' "$second" || {
  echo "Second Panel reconciliation was not idempotent" >&2
  exit 1
}

python - "$state" <<'PY'
import json
import pathlib
import sys

state = json.loads(pathlib.Path(sys.argv[1]).read_text())
assert state["keygen_calls"] == 1, state
PY
