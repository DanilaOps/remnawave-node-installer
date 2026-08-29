#!/usr/bin/env bash
# Config Profile, Xray JSON template and Internal Squad are three different
# objects in three different endpoints. Confusing any two of them is not a
# theoretical mistake: a name that belongs to one was already fed to the lookup
# of another, and the run failed complaining about the wrong thing entirely.
#
# Each case below breaks exactly one of the three and requires the run to fail
# naming that one - and never one of the other two.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo="$(cd "$root/.." && pwd)"
state="$(mktemp)"
output="$(mktemp)"
node_env="$(mktemp)"
rm -f "$node_env"

cleanup() {
  kill "${server_pid:-}" 2>/dev/null || true
  rm -f "$state" "$output" "$node_env"
}
trap cleanup EXIT

python "$root/tests/seed_shared_profile.py" "$state"
python "$root/tests/mock_panel.py" --port 18082 --state "$state" &
server_pid=$!
for _ in $(seq 1 30); do
  curl --silent --fail http://127.0.0.1:18082/api/nodes >/dev/null && break
  sleep 0.1
done

run() {
  ANSIBLE_CONFIG="$repo/ansible.cfg" ansible-playbook \
    -i localhost, -c local "$root/tests/panel_idempotency.yml" \
    -e remnawave_panel_url=http://127.0.0.1:18082 \
    -e test_node_env_path="$node_env" "$@" >"$output" 2>&1
}

# $1 human label, $2 phrase the failure must contain, $3.. phrases it must NOT
# contain, then -- and the playbook arguments. The forbidden phrases are the
# *accusation* forms - "<entity> '<name>' is" - because every message names the
# other two entities on purpose, to tell the operator what the object is not.
expect_failure_about() {
  local label="$1" wanted="$2"; shift 2
  local -a forbidden=()
  while [ "$1" != "--" ]; do forbidden+=("$1"); shift; done
  shift
  if run "$@"; then
    echo "Expected the $label case to fail" >&2; cat "$output" >&2; exit 1
  fi
  if ! grep -Fq "$wanted" "$output"; then
    echo "The $label failure has to say: $wanted" >&2
    cat "$output" >&2
    exit 1
  fi
  for phrase in "${forbidden[@]}"; do
    if grep -Fq "$phrase" "$output"; then
      echo "The $label failure blamed the wrong entity: $phrase" >&2
      cat "$output" >&2
      exit 1
    fi
  done
  # An unmatched grep above must not become this function's exit status: with
  # set -e that would end the run as if the assertion had failed.
  return 0
}

# 1. A name that exists only as an Xray JSON template must not be accepted as a
#    Config Profile. This is the exact mix-up that started this: 'Mock Xray
#    Template' is a Subscription Template, not a Config Profile.
expect_failure_about "template name used as a Config Profile" \
  "Config Profile 'Mock Xray Template' is absent" \
  "Xray JSON template 'Mock Xray Template' is absent" -- \
  -e '{"profile_name": "Mock Xray Template"}'

# 2. A missing Config Profile blames the Config Profile.
expect_failure_about "missing Config Profile" \
  "Config Profile 'NO-SUCH-PROFILE' is absent" \
  "Xray JSON template '" "Internal Squad '" -- \
  -e profile_name=NO-SUCH-PROFILE

# 3. A missing Xray JSON template blames the template, not the profile.
expect_failure_about "missing Xray JSON template" \
  "Xray JSON template 'NO-SUCH-TEMPLATE' is" \
  "Config Profile 'Mock-Profile' is absent" -- \
  -e xray_json_template_name=NO-SUCH-TEMPLATE

# 4. A template that exists under another type is not an XRAY_JSON template. The
#    seed holds 'Mock Xray Template' twice: XRAY_JSON and MIHOMO.
run -e '{"xray_json_template_name": "Mock Xray Template"}' || {
  echo "The XRAY_JSON template in the seed must be found" >&2; cat "$output" >&2; exit 1
}

# 5. A missing Internal Squad blames the Squad.
expect_failure_about "missing Internal Squad" \
  "Internal Squad 'NO-SUCH-SQUAD' is absent" \
  "Config Profile '" "Xray JSON template '" -- \
  -e internal_squad_name=NO-SUCH-SQUAD -e internal_squad_create=false

# 6. Idempotency with all three links in place, then drift on the template only.
run
grep -Eq 'changed=0([[:space:]]|$)' "$output" || {
  echo "A second reconciliation with all three links in place must change nothing" >&2
  cat "$output" >&2; exit 1
}

before="$(python - "$state" <<'PY'
import json, pathlib, sys
state = json.loads(pathlib.Path(sys.argv[1]).read_text())
template = next(t for t in state["templates"]
                if t["templateType"] == "XRAY_JSON" and t["name"] == "Mock Xray Template")
host = state["hosts"][0]
assert host["xrayJsonTemplateUuid"] == template["uuid"], host
print(json.dumps({"node": state["nodes"][0]["uuid"], "host": host["uuid"],
                  "hosts": len(state["hosts"]), "nodes": len(state["nodes"])}))
PY
)"

# Drift on the template link only, applied through the API: the mock keeps its
# state in memory and would overwrite an edit made to the file behind its back.
host_uuid="$(python -c 'import json,sys; print(json.loads(sys.argv[1])["host"])' "$before")"
curl --silent --show-error --fail -X PATCH http://127.0.0.1:18082/api/hosts \
  -H 'Content-Type: application/json' \
  -d "{\"uuid\": \"$host_uuid\", \"xrayJsonTemplateUuid\": \"99999999-9999-9999-9999-999999999999\"}" \
  >/dev/null

run
grep -Eq 'changed=[1-9]' "$output" || {
  echo "A Host pointing at the wrong Xray JSON template must be corrected" >&2
  cat "$output" >&2; exit 1
}

python - "$state" "$before" <<'PY'
import json, pathlib, sys
state = json.loads(pathlib.Path(sys.argv[1]).read_text())
before = json.loads(sys.argv[2])
template = next(t for t in state["templates"]
                if t["templateType"] == "XRAY_JSON" and t["name"] == "Mock Xray Template")
host = state["hosts"][0]
assert host["xrayJsonTemplateUuid"] == template["uuid"], host
# Only the link changed: nothing was recreated.
assert host["uuid"] == before["host"], "the Host was recreated instead of updated"
assert state["nodes"][0]["uuid"] == before["node"], "the Node was recreated"
assert len(state["hosts"]) == before["hosts"], state["hosts"]
assert len(state["nodes"]) == before["nodes"], state["nodes"]
print("Template drift corrected the link and recreated nothing.")
PY

echo "Config Profile, Xray JSON template and Internal Squad are three separate lookups."
