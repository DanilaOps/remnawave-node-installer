#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
state="$(mktemp)"
first="$(mktemp)"
second="$(mktemp)"
node_env="$(mktemp)"
rm -f "$state" "$node_env"

cleanup() {
  kill "${server_pid:-}" 2>/dev/null || true
  rm -f "$state" "$first" "$second" "$node_env"
}
trap cleanup EXIT

python "$root/tests/mock_panel.py" --port 18082 --state "$state" &
server_pid=$!
for _ in $(seq 1 30); do
  curl --silent --fail http://127.0.0.1:18082/api/nodes >/dev/null && break
  sleep 0.1
done

curl --silent --fail \
  -X POST http://127.0.0.1:18082/api/users \
  -H 'Content-Type: application/json' \
  -d '{"username":"bridge_mock_01","ssPassword":"stable-bridge-password","activeInternalSquads":[]}' \
  >/dev/null

for output in "$first" "$second"; do
  ANSIBLE_CONFIG="$root/ansible.cfg" ansible-playbook \
    -i localhost, -c local "$root/tests/panel_idempotency.yml" \
    -e remnawave_panel_url=http://127.0.0.1:18082 \
    -e test_node_env_path="$node_env" \
    -e @"$root/tests/bridge_vars.yml" | tee "$output"
done

grep -Eq 'changed=0([[:space:]]|$)' "$second" || {
  echo "Second bridge reconciliation was not idempotent" >&2
  exit 1
}

python - "$state" <<'PY'
import json
import pathlib
import sys

state = json.loads(pathlib.Path(sys.argv[1]).read_text())
assert state["keygen_calls"] == 1, state
assert len(state["users"]) == 1, state
user = state["users"][0]
assert user["username"] == "bridge_mock_01", user
assert user["ssPassword"] == "stable-bridge-password", user
assert len(user["activeInternalSquads"]) == 1, user
PY
