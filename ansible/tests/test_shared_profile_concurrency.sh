#!/usr/bin/env bash
# A Config Profile is reconciled with a read-modify-write, so
# the dangerous failure is a write that succeeds and silently drops another
# node's inbound. Three scenarios, all against the stateful mock panel:
#
#   1. a second writer changes the profile between this run's read and its write
#      -> the run must refuse and write nothing;
#   2. the same run with nobody interfering -> the foreign inbound survives;
#   3. inbound_prune_tags naming somebody else's inbound -> refused before any
#      request is made.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo="$(cd "$root/.." && pwd)"
work="$(mktemp -d)"
server_pid=""

cleanup() {
  [[ -n "$server_pid" ]] && kill "$server_pid" 2>/dev/null || true
  rm -rf "$work"
}
trap cleanup EXIT

start_panel() { # start_panel <state> [extra args...]
  local state="$1"; shift
  python "$root/tests/mock_panel.py" --state "$state" "$@" &
  server_pid=$!
  for _ in $(seq 1 30); do
    curl --silent --fail http://127.0.0.1:18080/api/nodes >/dev/null && return 0
    sleep 0.1
  done
  echo "mock panel did not start" >&2
  return 1
}

stop_panel() {
  [[ -n "$server_pid" ]] && kill "$server_pid" 2>/dev/null || true
  wait "$server_pid" 2>/dev/null || true
  server_pid=""
}

run_panel_role() { # run_panel_role <logfile> [extra ansible args...]
  local log="$1"; shift
  ANSIBLE_CONFIG="$repo/ansible.cfg" ansible-playbook \
    -i localhost, -c local "$root/tests/panel_idempotency.yml" \
    -e test_node_env_path="$work/node.env" "$@" >"$log" 2>&1
}

# --- 1. a concurrent writer must not be overwritten -------------------------
state="$work/state-concurrent.json"
python "$root/tests/seed_shared_profile.py" "$state"
start_panel "$state" --mutate-after-profile-read 1

if run_panel_role "$work/concurrent.log"; then
  echo "FAIL: a concurrent change to the shared profile was overwritten silently" >&2
  tail -40 "$work/concurrent.log" >&2
  exit 1
fi
grep -q "changed between the moment this run read it" "$work/concurrent.log" || {
  echo "FAIL: the run failed, but not on the concurrent-change guard" >&2
  tail -40 "$work/concurrent.log" >&2
  exit 1
}
python - "$state" <<'PY'
import json, pathlib, sys
state = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
tags = [i["tag"] for i in state["profiles"][0]["config"]["inbounds"]]
assert "OTHER_NODE_REALITY" in tags, tags
assert "CONCURRENT_NODE_REALITY" in tags, tags
assert "MOCK_01_REALITY" not in tags, f"the refused run still wrote its inbound: {tags}"
assert state["nodes"] == [], "a Node was created even though the profile write was refused"
print("   nothing was written: %s" % tags)
PY
stop_panel
echo "OK  1/3 a concurrent profile change is refused, not overwritten"

# --- 2. without interference the neighbour's inbound survives ---------------
state="$work/state-clean.json"
python "$root/tests/seed_shared_profile.py" "$state"
start_panel "$state"
run_panel_role "$work/clean.log" || {
  echo "FAIL: the undisturbed run did not succeed" >&2
  tail -40 "$work/clean.log" >&2
  exit 1
}
python - "$state" <<'PY'
import json, pathlib, sys
state = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
config = state["profiles"][0]["config"]
tags = [i["tag"] for i in config["inbounds"]]
assert "OTHER_NODE_REALITY" in tags, tags
assert "MOCK_01_REALITY" in tags, tags
foreign = next(i for i in config["inbounds"] if i["tag"] == "OTHER_NODE_REALITY")
assert foreign["streamSettings"]["realitySettings"]["privateKey"].startswith("FOREIGN")
assert config["routing"]["rules"], "routing was lost"
assert config["outbounds"], "outbounds were lost"
print("   merged: %s" % tags)
PY
stop_panel
echo "OK  2/3 the neighbour's inbound, key, routing and outbounds survive the merge"

# --- 3. pruning somebody else's inbound is refused --------------------------
state="$work/state-prune.json"
python "$root/tests/seed_shared_profile.py" "$state"
start_panel "$state"
if run_panel_role "$work/prune.log" -e '{"inbound_prune_tags": ["OTHER_NODE_REALITY"]}'; then
  echo "FAIL: pruning another node's inbound was allowed" >&2
  exit 1
fi
grep -q "may only name inbounds this node owns" "$work/prune.log" || {
  echo "FAIL: the run failed, but not on the prune-ownership guard" >&2
  tail -40 "$work/prune.log" >&2
  exit 1
}
python - "$state" <<'PY'
import json, pathlib, sys
state = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
tags = [i["tag"] for i in state["profiles"][0]["config"]["inbounds"]]
assert "OTHER_NODE_REALITY" in tags, tags
PY
stop_panel
echo "OK  3/3 inbound_prune_tags cannot name another node's inbound"

echo "Shared Config Profile is protected against concurrent writes and cross-node pruning."
