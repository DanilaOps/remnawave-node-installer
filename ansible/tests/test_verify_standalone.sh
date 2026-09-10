#!/usr/bin/env bash
# "03 - Verify Node" is its own Semaphore template and therefore its own Ansible
# run: not one fact the reconciler set survives into it. On tr01 that surfaced
# as an undefined remnawave_api_base, with every other reconciler fact lined up
# to fail next. This proves the standalone contract end to end:
#
#   1. the reconciler publishes the node into the mock Panel (first run);
#   2. a FRESH ansible-playbook invocation verifies it, resolving every
#      reference itself, read-only - the state file must not change by a byte;
#   3. a Panel missing the Node fails with instructions to run 02, and creates
#      nothing.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo="$(cd "$root/.." && pwd)"
state="$(mktemp)"
node_work="$(mktemp -d)"
out="$(mktemp)"
cleanup() {
  kill "${server_pid:-}" 2>/dev/null || true
  rm -rf "$state" "$node_work" "$out"
}
trap cleanup EXIT

# The mock loads its state file once at startup, so every edit made behind its
# back needs a restart to become visible - the price of keeping the mock simple.
start_server() {
  python "$root/tests/mock_panel.py" --state "$state" &
  server_pid=$!
  for _ in $(seq 1 30); do
    curl --silent --fail http://127.0.0.1:18080/api/nodes >/dev/null 2>&1 && break
    sleep 0.1
  done
}
stop_server() {
  kill "${server_pid:-}" 2>/dev/null || true
  wait "${server_pid:-}" 2>/dev/null || true
}

python "$root/tests/seed_shared_profile.py" "$state"
start_server

# Run 1: the reconciler publishes the node - a separate Ansible process, exactly
# like Semaphore template 02.
ANSIBLE_CONFIG="$repo/ansible.cfg" ansible-playbook \
  -i localhost, -c local "$root/tests/panel_idempotency.yml" \
  -e test_node_env_path="$node_work/identity/.env" \
  -e "remnawave_node_identity_owner=$(id -un)" \
  -e "remnawave_node_identity_group=$(id -gn)" >/dev/null

# The mock's Node was just created, so it reports what a freshly reconciled node
# reports; acceptance additionally needs the runtime fields a live node carries.
python - "$state" <<'ONLINE'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
state = json.loads(path.read_text())
node = state["nodes"][0]
node.update({"isConnected": True, "xrayUptime": 42,
             "versions": {"xray": "26.6.27", "node": "mock"}})
path.write_text(json.dumps(state))
ONLINE
stop_server
start_server

# Semantic comparison, not a byte one: the mock rewrites its state file with
# its own formatting on some reads, but the DATA must not change.
normalized() {
  python3 -c "import json,sys; print(json.dumps(json.load(open(sys.argv[1])), sort_keys=True))" "$1"
}
before="$(normalized "$state")"

# What the reconciler actually published for this node's inbound. Acceptance
# has to resolve exactly this, not merely something non-empty.
expected_key="$(python3 -c "
import json,sys
state=json.load(open(sys.argv[1]))
inbound=[i for p in state['profiles'] for i in p['config']['inbounds'] if i['tag']=='MOCK_01_REALITY'][0]
print(inbound['streamSettings']['realitySettings']['privateKey'])
" "$state")"
expected_short_ids="$(python3 -c "
import json,sys
state=json.load(open(sys.argv[1]))
inbound=[i for p in state['profiles'] for i in p['config']['inbounds'] if i['tag']=='MOCK_01_REALITY'][0]
print(json.dumps(inbound['streamSettings']['realitySettings']['shortIds']))
" "$state")"

# Run 2: standalone acceptance - a fresh process, no inherited facts.
ANSIBLE_CONFIG="$repo/ansible.cfg" ansible-playbook \
  -i localhost, -c local "$root/tests/verify_standalone.yml" \
  -e test_expected_reality_key="$expected_key" \
  -e "{\"test_expected_short_ids\": $expected_short_ids}" | tee "$out"
grep -qE 'failed=0' "$out" || { echo "standalone verify failed" >&2; exit 1; }

after="$(normalized "$state")"
if [ "$before" != "$after" ]; then
  echo "standalone verify MUTATED the Panel state - it must be read-only" >&2
  exit 1
fi
echo "Standalone verify resolved everything itself and wrote nothing."

# Run 3: the Node is gone - acceptance must fail with instructions, not create it.
python - "$state" <<'GONE'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
state = json.loads(path.read_text())
state["nodes"] = []
path.write_text(json.dumps(state))
GONE
stop_server
start_server
if ANSIBLE_CONFIG="$repo/ansible.cfg" ansible-playbook \
    -i localhost, -c local "$root/tests/verify_standalone.yml" \
    -e test_expected_reality_key="$expected_key" \
    -e "{\"test_expected_short_ids\": $expected_short_ids}" > "$out" 2>&1; then
  echo "verify passed against a Panel with no Node" >&2
  exit 1
fi
grep -qF '02 - Install / Reconcile Node' "$out" || {
  echo "the missing-Node failure does not tell the operator to run 02" >&2
  tail -20 "$out"
  exit 1
}
python - "$state" <<'EMPTY'
import json, pathlib, sys
state = json.loads(pathlib.Path(sys.argv[1]).read_text())
assert state["nodes"] == [], "verify CREATED a Node instead of failing"
EMPTY
echo "A missing Node fails with instructions and is never created."
