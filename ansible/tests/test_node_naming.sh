#!/usr/bin/env bash
# Naming: <COUNTRY>-NN, allocated once from what the panel already holds and then
# living in the inventory hostname. The two failures this guards against are the
# expensive ones: a re-run of an existing node deciding it is a new one, and a
# freed number being handed to a different machine.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo="$(cd "$root/.." && pwd)"
state="$(mktemp)"
output="$(mktemp)"

cleanup() {
  kill "${server_pid:-}" 2>/dev/null || true
  rm -f "$state" "$output"
}
trap cleanup EXIT

seed() {
  python - "$state" "$@" <<'PY'
import json, pathlib, sys
names = sys.argv[2:]
state = {
    "profiles": [], "nodes": [], "hosts": [], "squads": [], "users": [],
    "templates": [], "keygen_calls": 0,
}
for entry in names:
    kind, name = entry.split(":", 1)
    if kind == "node":
        state["nodes"].append({"uuid": f"n-{name}", "name": name, "address": "203.0.113.1"})
    elif kind == "profile":
        # A real profile carries routing; an empty one is refused by the role on
        # purpose, and that is a different test.
        state["profiles"].append({
            "uuid": f"p-{name}",
            "name": name,
            "config": {
                "inbounds": [],
                "outbounds": [{"tag": "DIRECT", "protocol": "freedom"}],
                "routing": {"rules": [{"type": "field", "outboundTag": "DIRECT", "network": "tcp,udp"}]},
            },
        })
    elif kind == "host":
        state["hosts"].append({"uuid": f"h-{name}", "remark": name, "address": "x"})
pathlib.Path(sys.argv[1]).write_text(json.dumps(state))
PY
}

start_panel() {
  kill "${server_pid:-}" 2>/dev/null || true
  python "$root/tests/mock_panel.py" --port 18083 --state "$state" &
  server_pid=$!
  for _ in $(seq 1 30); do
    curl --silent --fail http://127.0.0.1:18083/api/nodes >/dev/null && return 0
    sleep 0.1
  done
  echo "mock panel did not start" >&2
  exit 1
}

allocate() {
  ANSIBLE_CONFIG="$repo/ansible.cfg" ansible-playbook \
    -i localhost, -c local "$root/playbooks/next_node_name.yml" \
    -e remnawave_panel_url=http://127.0.0.1:18083 \
    -e remnawave_panel_token=mock-token \
    -e node_domain_zone=example.test \
    -e "country_code=$1" >"$output" 2>&1
}

expect_name() {
  local country="$1" wanted="$2"; shift 2
  seed "$@"
  start_panel
  if ! allocate "$country"; then
    echo "Allocation for $country failed" >&2; cat "$output" >&2; exit 1
  fi
  if ! grep -Fq "next node name     = $wanted" "$output"; then
    echo "Expected $wanted for $country with [$*]" >&2; cat "$output" >&2; exit 1
  fi
}

# 1. No nodes of this country yet.
expect_name TR TR-01
# 2. One in use.
expect_name TR TR-02 node:TR-01
# 3. A gap is not reused: TR-03 existed once, so TR-04 is next.
expect_name TR TR-04 node:TR-01 node:TR-03
# 4. Other countries do not shift the count.
expect_name TR TR-02 node:DE-01 node:DE-02 node:TR-01
# 5. A number is taken when any object still carries it, not only a Node. A
#    Config Profile left behind by a deleted node is exactly that case.
expect_name TR TR-03 profile:TR-01 profile:TR-02
expect_name TR TR-02 host:TR-01
# 6. Names that merely start with the country code are not this fleet's naming.
expect_name TR TR-01 node:TREX node:TR-EDGE

# 7. An unknown country is refused rather than published with an empty label.
seed
start_panel
if allocate ZZ; then
  echo "A country with no display name must not be allocated" >&2; cat "$output" >&2; exit 1
fi
grep -Fq "has no display name" "$output" || {
  echo "The refusal has to name the missing country label" >&2; cat "$output" >&2; exit 1
}

# 8. Allocation never writes anything: the panel must be untouched.
seed node:TR-01
start_panel
allocate TR
python - "$state" <<'PY'
import json, pathlib, sys
state = json.loads(pathlib.Path(sys.argv[1]).read_text())
assert [n["name"] for n in state["nodes"]] == ["TR-01"], state["nodes"]
assert state["profiles"] == [], state["profiles"]
assert state["hosts"] == [], state["hosts"]
print("Allocation read the panel and changed nothing.")
PY

# 9. In check mode it still reports a name, and still writes nothing.
seed node:TR-01
start_panel
ANSIBLE_CONFIG="$repo/ansible.cfg" ansible-playbook \
  -i localhost, -c local "$root/playbooks/next_node_name.yml" \
  -e remnawave_panel_url=http://127.0.0.1:18083 \
  -e remnawave_panel_token=mock-token \
  -e node_domain_zone=example.test \
  -e country_code=TR --check --diff >"$output" 2>&1
grep -Fq "next node name     = TR-02" "$output" || {
  echo "A dry-run has to allocate the same name" >&2; cat "$output" >&2; exit 1
}
grep -Eq 'changed=0([[:space:]]|$)' "$output" || {
  echo "Allocation must report no change at all" >&2; cat "$output" >&2; exit 1
}

# This test is about naming, so the Xray JSON template link is left unmanaged;
# ansible/tests/test_panel_entities.sh is what covers that link.
# 10. Reconciling an existing node keeps its identity. The reconcile path derives
#     the name from the inventory hostname and never asks the allocator, so a
#     second run of tr01 is TR-01 again - not "TR-01 exists, so TR-02 is next".
# The role hardens the directory of the identity file to 0700 and one owner, so
# the fixture gives it a directory of its own inside a workspace this test
# created. A bare mktemp file would make that directory /tmp itself.
node_work="$(mktemp -d)"
node_env="$node_work/identity/.env"
trap 'kill "${server_pid:-}" 2>/dev/null || true; rm -rf "$state" "$output" "$node_work"' EXIT

reconcile() {
  ANSIBLE_CONFIG="$repo/ansible.cfg" ansible-playbook \
    -i localhost, -c local "$root/tests/panel_idempotency.yml" \
    -e remnawave_panel_url=http://127.0.0.1:18083 \
    -e test_node_env_path="$node_env" \
    -e "remnawave_node_identity_owner=$(id -un)" \
    -e "remnawave_node_identity_group=$(id -gn)" \
    -e node_id=tr_01 -e node_name=TR-01 -e profile_name=TR-01 \
    -e config_profile_create=true -e node_country=TR \
    -e xray_json_template_name= \
    -e '{"inbound_specs": [{"tag": "TR_01_REALITY", "port": 443, "network": "raw"}]}' \
    -e '{"host_specs": [{"remark": "Turkey", "address": "tr01.example.test", "inbound_tag": "TR_01_REALITY", "port": 443, "sni": "tr01.example.test", "fingerprint": "firefox", "security_layer": "DEFAULT", "tags": ["TR", "DIRECT"]}]}' \
    >"$output" 2>&1
}

seed
start_panel
reconcile || { echo "First reconcile of TR-01 failed" >&2; cat "$output" >&2; exit 1; }
reconcile || { echo "Second reconcile of TR-01 failed" >&2; cat "$output" >&2; exit 1; }
grep -Eq 'changed=0([[:space:]]|$)' "$output" || {
  echo "Reconciling the same node twice must change nothing" >&2; cat "$output" >&2; exit 1
}
python - "$state" <<'PYCHECK'
import json, pathlib, sys
state = json.loads(pathlib.Path(sys.argv[1]).read_text())
names = sorted(n["name"] for n in state["nodes"])
profiles = sorted(p["name"] for p in state["profiles"])
assert names == ["TR-01"], f"a second run invented another node: {names}"
assert profiles == ["TR-01"], f"a second run invented another profile: {profiles}"
print("Re-running tr01 reconciles TR-01 and allocates nothing.")
PYCHECK

# 11. A Node exists and its Config Profile does not: the run reconciles TR-01 and
#     creates the missing profile, rather than deciding TR-02 is next.
seed node:TR-01
start_panel
reconcile || { echo "Reconcile with a missing Config Profile failed" >&2; cat "$output" >&2; exit 1; }
python - "$state" <<'PYCHECK'
import json, pathlib, sys
state = json.loads(pathlib.Path(sys.argv[1]).read_text())
assert sorted(n["name"] for n in state["nodes"]) == ["TR-01"], state["nodes"]
assert sorted(p["name"] for p in state["profiles"]) == ["TR-01"], state["profiles"]
print("A Node without its Config Profile is reconciled, not renumbered.")
PYCHECK

# 12. The reverse: a Config Profile named TR-01 exists with no Node. It is this
#     project's own object from an interrupted run, so it is adopted - and the
#     run must not create a duplicate profile beside it.
seed profile:TR-01
start_panel
reconcile || { echo "Reconcile with an orphaned Config Profile failed" >&2; cat "$output" >&2; exit 1; }
python - "$state" <<'PYCHECK'
import json, pathlib, sys
state = json.loads(pathlib.Path(sys.argv[1]).read_text())
profiles = [p["name"] for p in state["profiles"]]
assert profiles == ["TR-01"], f"the existing profile was duplicated: {profiles}"
assert sorted(n["name"] for n in state["nodes"]) == ["TR-01"], state["nodes"]
print("An orphaned Config Profile is adopted, not duplicated.")
PYCHECK

echo "Node names are allocated once, monotonically, and never reused."
