#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo="$(cd "$root/.." && pwd)"
state="$(mktemp)"
first="$(mktemp)"
second="$(mktemp)"
# The role hardens the directory of the identity file to 0700 and one owner, so
# the fixture gives it a directory of its own inside a workspace this test
# created. A bare mktemp file would make that directory /tmp itself.
node_work="$(mktemp -d)"
node_env="$node_work/identity/.env"
cleanup() {
  kill "${server_pid:-}" 2>/dev/null || true
  rm -rf "$state" "$first" "$second" "$node_work"
}
trap cleanup EXIT

# The Config Profile already exists and carries the routing, which is the state
# production is in, so the test starts from there instead of an empty panel.
python "$root/tests/seed_shared_profile.py" "$state"

python "$root/tests/mock_panel.py" --state "$state" &
server_pid=$!

for _ in $(seq 1 30); do
  curl --silent --fail http://127.0.0.1:18080/api/nodes >/dev/null && break
  sleep 0.1
done

ANSIBLE_CONFIG="$repo/ansible.cfg" ansible-playbook \
  -i localhost, -c local "$root/tests/panel_idempotency.yml" \
  -e test_node_env_path="$node_env" \
  -e "remnawave_node_identity_owner=$(id -un)" \
  -e "remnawave_node_identity_group=$(id -gn)" | tee "$first"
ANSIBLE_CONFIG="$repo/ansible.cfg" ansible-playbook \
  -i localhost, -c local "$root/tests/panel_idempotency.yml" \
  -e test_node_env_path="$node_env" \
  -e "remnawave_node_identity_owner=$(id -un)" \
  -e "remnawave_node_identity_group=$(id -gn)" | tee "$second"

grep -Eq 'changed=0([[:space:]]|$)' "$second" || {
  echo "Second Panel reconciliation was not idempotent" >&2
  exit 1
}

# tr01 drift: the panel still holds the Host with the selfsteal domain as its
# address, from before the fleet published the node's IP. The next reconcile
# must repair that Host in place - same UUID, new address - not leave it behind
# and create a second one.
third="$(mktemp)"
drift_uuid="$(python - "$state" <<'DRIFT'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
state = json.loads(path.read_text())
assert len(state["hosts"]) == 1, state["hosts"]
state["hosts"][0]["address"] = "node.example.test"
path.write_text(json.dumps(state))
print(state["hosts"][0]["uuid"])
DRIFT
)"
ANSIBLE_CONFIG="$repo/ansible.cfg" ansible-playbook \
  -i localhost, -c local "$root/tests/panel_idempotency.yml" \
  -e test_node_env_path="$node_env" \
  -e "remnawave_node_identity_owner=$(id -un)" \
  -e "remnawave_node_identity_group=$(id -gn)" | tee "$third"
python - "$state" "$drift_uuid" <<'REPAIRED'
import json, pathlib, sys
state = json.loads(pathlib.Path(sys.argv[1]).read_text())
hosts = state["hosts"]
assert len(hosts) == 1, f"the re-addressed Host was duplicated: {hosts}"
assert hosts[0]["uuid"] == sys.argv[2], "the Host was recreated instead of repaired"
assert hosts[0]["address"] == "203.0.113.10", hosts[0]["address"]
assert hosts[0]["sni"] == "node.example.test", hosts[0]["sni"]
print("A re-addressed Host is repaired in place, never duplicated.")
REPAIRED

python - "$state" "$root/tests" <<'PY'
import json
import pathlib
import sys

sys.path.insert(0, sys.argv[2])

from seed_shared_profile import (
    FOREIGN_INBOUND_UUID,
    FOREIGN_PRIVATE_KEY,
    PROFILE_UUID,
)

state = json.loads(pathlib.Path(sys.argv[1]).read_text())

assert state["keygen_calls"] == 1, state["keygen_calls"]

profiles = state["profiles"]
assert len(profiles) == 1, "the shared profile must not be duplicated"
profile = profiles[0]
assert profile["uuid"] == PROFILE_UUID, profile["uuid"]
assert profile["name"] == "Mock-Profile", profile["name"]

config = profile["config"]
rules = config["routing"]["rules"]
assert len(rules) == 2, f"routing was modified: {rules}"
assert config["outbounds"] == [{"tag": "DIRECT", "protocol": "freedom"}], config["outbounds"]

inbounds = {inbound["tag"]: inbound for inbound in config["inbounds"]}
assert set(inbounds) == {"OTHER_NODE_REALITY", "MOCK_01_REALITY"}, set(inbounds)

foreign = inbounds["OTHER_NODE_REALITY"]
assert foreign["uuid"] == FOREIGN_INBOUND_UUID, "the foreign inbound lost its identity"
foreign_reality = foreign["streamSettings"]["realitySettings"]
assert foreign_reality["privateKey"] == FOREIGN_PRIVATE_KEY, "foreign Reality key was rewritten"
assert foreign_reality["serverNames"] == ["other.example.test"], foreign_reality["serverNames"]

managed = inbounds["MOCK_01_REALITY"]
managed_reality = managed["streamSettings"]["realitySettings"]
assert managed_reality["privateKey"] != FOREIGN_PRIVATE_KEY, "adopted another node's Reality key"
assert managed_reality["serverNames"] == ["node.example.test"], managed_reality["serverNames"]

hosts = state["hosts"]
assert len(hosts) == 1, hosts
assert hosts[0]["inbound"]["configProfileUuid"] == PROFILE_UUID, hosts[0]["inbound"]
assert hosts[0]["inbound"]["configProfileInboundUuid"] == managed["uuid"], hosts[0]["inbound"]

nodes = state["nodes"]
assert len(nodes) == 1, nodes
active = nodes[0]["configProfile"]
assert active["activeConfigProfileUuid"] == PROFILE_UUID, active
assert active["activeInbounds"] == [managed["uuid"]], active

print("Shared profile merge, Host binding and Node activation are correct.")
PY
