#!/usr/bin/env bash
# The end-to-end probe is what makes "ready" mean something, so its own failure
# modes are checked here: fail-closed with no identity, refusal when the probe
# user is not usable on this node, and a full pass through a real SOCKS proxy to
# a real HTTP endpoint.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo="$(cd "$root/.." && pwd)"
work="$(mktemp -d)"
panel_pid=""
http_pid=""
socks_port=$(( 11100 + RANDOM % 400 ))
http_port=$(( 11600 + RANDOM % 400 ))

cleanup() {
  [[ -n "$panel_pid" ]] && kill "$panel_pid" 2>/dev/null || true
  [[ -n "$http_pid" ]] && kill "$http_pid" 2>/dev/null || true
  pkill -f "remnawave-probe-mock_01.json" 2>/dev/null || true
  rm -rf "$work"
}
trap cleanup EXIT

squad_uuid="44444444-4444-4444-4444-444444444444"
probe_uuid="55555555-5555-5555-5555-555555555555"

seed() { # seed <status> <squad-uuid> <user-uuid>
  python - "$work/state.json" "$1" "$2" "$3" <<'PY'
import json, pathlib, sys
path, status, squad, user_uuid = sys.argv[1:5]
pathlib.Path(path).write_text(json.dumps({
    "profiles": [], "nodes": [], "hosts": [],
    "squads": [{"uuid": squad, "name": "Mock Squad", "inbounds": []}],
    "users": [{
        "uuid": user_uuid, "id": 1, "username": "probe", "status": status,
        "activeInternalSquads": [{"uuid": squad}],
    }],
    "keygen_calls": 0,
}, indent=2), encoding="utf-8")
PY
}

start_panel() {
  python "$root/tests/mock_panel.py" --state "$work/state.json" &
  panel_pid=$!
  for _ in $(seq 1 30); do
    curl --silent --fail http://127.0.0.1:18080/api/nodes >/dev/null && return 0
    sleep 0.1
  done
  echo "mock panel did not start" >&2; return 1
}

stop_panel() { [[ -n "$panel_pid" ]] && kill "$panel_pid" 2>/dev/null || true; wait "$panel_pid" 2>/dev/null || true; panel_pid=""; }

start_http() {
  python - "$http_port" <<'PY' &
import http.server, sys
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()
    def log_message(self, *_args):
        return
http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
PY
  http_pid=$!
  for _ in $(seq 1 30); do
    curl --silent --output /dev/null "http://127.0.0.1:$http_port/" && return 0
    sleep 0.1
  done
  echo "local HTTP endpoint did not start" >&2; return 1
}

run_probe() { # run_probe <logfile> [extra -e args...]
  local log="$1"; shift
  ANSIBLE_CONFIG="$repo/ansible.cfg" ansible-playbook \
    -i localhost, -c local "$root/tests/tunnel_probe.yml" \
    -e test_workdir="$work" \
    -e test_xray_binary="$root/tests/fake_socks_xray.py" \
    -e test_socks_port="$socks_port" \
    -e test_probe_url="http://127.0.0.1:$http_port/generate_204" \
    -e test_squad_uuid="$squad_uuid" \
    "$@" >"$log" 2>&1
}

chmod +x "$root/tests/fake_socks_xray.py"
start_http

# --- 1. no identity configured -> fail closed -------------------------------
seed ACTIVE "$squad_uuid" "$probe_uuid"
start_panel
if run_probe "$work/none.log" -e test_probe_uuid=""; then
  echo "FAIL: strict acceptance passed without any probe identity" >&2
  exit 1
fi
grep -q "Strict acceptance needs an end-to-end probe" "$work/none.log" || {
  echo "FAIL: failed for the wrong reason" >&2; tail -30 "$work/none.log" >&2; exit 1; }
stop_panel
echo "OK  1/4 no probe identity -> the run fails instead of reporting a ready node"

# --- 2. probe user disabled -> refused --------------------------------------
seed DISABLED "$squad_uuid" "$probe_uuid"
start_panel
if run_probe "$work/disabled.log" -e test_probe_uuid="$probe_uuid"; then
  echo "FAIL: a DISABLED probe user was accepted" >&2
  exit 1
fi
grep -q "cannot prove this node" "$work/disabled.log" || {
  echo "FAIL: failed for the wrong reason" >&2; tail -30 "$work/disabled.log" >&2; exit 1; }
stop_panel
echo "OK  2/4 a probe user that is not ACTIVE is refused"

# --- 3. probe user in another squad -> refused ------------------------------
seed ACTIVE "99999999-9999-9999-9999-999999999999" "$probe_uuid"
start_panel
if run_probe "$work/squad.log" -e test_probe_uuid="$probe_uuid"; then
  echo "FAIL: a probe user without access to this node's squad was accepted" >&2
  exit 1
fi
grep -q "cannot prove this node" "$work/squad.log" || {
  echo "FAIL: failed for the wrong reason" >&2; tail -30 "$work/squad.log" >&2; exit 1; }
stop_panel
echo "OK  3/4 a probe user outside this node's Internal Squad is refused"

# --- 4. everything in place -> a real request goes through the proxy --------
seed ACTIVE "$squad_uuid" "$probe_uuid"
start_panel
run_probe "$work/pass.log" -e test_probe_uuid="$probe_uuid" || {
  echo "FAIL: the probe did not complete against the stand-ins" >&2
  tail -40 "$work/pass.log" >&2; exit 1; }
grep -q "returned 204" "$work/pass.log" || {
  echo "FAIL: the probe passed without asserting the expected status" >&2
  tail -40 "$work/pass.log" >&2; exit 1; }
# The rendered client configuration must not survive the run.
[[ -z "$(find "$work" -name 'remnawave-probe-*.json' -print -quit)" ]] || {
  echo "FAIL: the probe left its client configuration behind" >&2; exit 1; }
stop_panel
echo "OK  4/4 a request through the proxy returns the expected status and the config is cleaned up"

echo "The end-to-end probe is fail-closed, checks the panel state and works end to end."
