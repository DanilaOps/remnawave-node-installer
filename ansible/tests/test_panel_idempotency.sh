#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo="$(cd "$root/.." && pwd)"
state="$(mktemp)"
first="$(mktemp)"
second="$(mktemp)"
node_env="$(mktemp)"
rm -f "$node_env"

cleanup() {
  kill "${server_pid:-}" 2>/dev/null || true
  rm -f "$state" "$first" "$second" "$node_env"
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
  -e test_node_env_path="$node_env" | tee "$first"
ANSIBLE_CONFIG="$repo/ansible.cfg" ansible-playbook \
  -i localhost, -c local "$root/tests/panel_idempotency.yml" \
  -e test_node_env_path="$node_env" | tee "$second"

grep -Eq 'changed=0([[:space:]]|$)' "$second" || {
  echo "Second Panel reconciliation was not idempotent" >&2
  exit 1
}

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
