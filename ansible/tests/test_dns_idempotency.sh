#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
state="$(mktemp)"
out="$(mktemp)"

cleanup() {
  kill "${server_pid:-}" 2>/dev/null || true
  rm -f "$state" "$out"
}
trap cleanup EXIT

start_server() {
  kill "${server_pid:-}" 2>/dev/null || true
  printf '%s' "$1" > "$state"
  python "$root/tests/mock_regru.py" --state "$state" &
  server_pid=$!
  for _ in $(seq 1 30); do
    curl --silent --output /dev/null \
      --data 'input_format=json&input_data={"username":"u","password":"p","domains":[{"dname":"example.com"}]}' \
      http://127.0.0.1:18083/zone/get_resource_records && break
    sleep 0.1
  done
}

run() {
  ANSIBLE_CONFIG="$root/ansible.cfg" ansible-playbook \
    -i localhost, -c local "$root/tests/dns_idempotency.yml" "$@" >"$out" 2>&1
}

expect_changed() {
  grep -Eq "changed=$1([[:space:]]|$)" "$out" || {
    echo "expected changed=$1, got:" >&2
    tail -20 "$out" >&2
    exit 1
  }
}

# 1. Empty zone: the record is created, and a second run leaves it alone.
start_server '{"zones": {"example.com": []}}'
run
expect_changed 1
run
expect_changed 0
python - "$state" <<'PY'
import json, pathlib, sys
records = json.loads(pathlib.Path(sys.argv[1]).read_text())["zones"]["example.com"]
assert records == [
    {"rectype": "A", "subname": "ee01", "content": "203.0.113.10", "state": "verified"}
], records
PY

# 2. Record points somewhere else: it is retargeted, then stable.
start_server '{"zones": {"example.com": [
  {"rectype": "A", "subname": "ee01", "content": "198.51.100.77", "state": "verified"},
  {"rectype": "A", "subname": "other", "content": "198.51.100.9", "state": "verified"},
  {"rectype": "MX", "subname": "@", "content": "mail.example.com", "state": "verified"}
]}}'
run
expect_changed 2
run
expect_changed 0
python - "$state" <<'PY'
import json, pathlib, sys
records = json.loads(pathlib.Path(sys.argv[1]).read_text())["zones"]["example.com"]
by_name = {(r["subname"], r["rectype"]): r["content"] for r in records}
assert by_name[("ee01", "A")] == "203.0.113.10", records
# Records this deployment does not manage must survive untouched.
assert by_name[("other", "A")] == "198.51.100.9", records
assert by_name[("@", "MX")] == "mail.example.com", records
assert len(records) == 3, records
PY

# 3. Two A records for the managed name: refuse to guess.
start_server '{"zones": {"example.com": [
  {"rectype": "A", "subname": "ee01", "content": "198.51.100.77", "state": "verified"},
  {"rectype": "A", "subname": "ee01", "content": "198.51.100.78", "state": "verified"}
]}}'
if run; then
  echo "expected the ambiguous-record case to fail" >&2
  exit 1
fi
grep -q "dns_prune_extra_records" "$out" || {
  echo "the failure did not explain how to resolve the ambiguity" >&2
  exit 1
}

# 4. With explicit consent the stale duplicates are removed.
run -e dns_prune_extra_records=true
# One looped removal task plus one creation task, whatever the number of stale
# records; the state assertion below is what proves both duplicates went away.
expect_changed 2
python - "$state" <<'PY'
import json, pathlib, sys
records = json.loads(pathlib.Path(sys.argv[1]).read_text())["zones"]["example.com"]
assert records == [
    {"rectype": "A", "subname": "ee01", "content": "203.0.113.10", "state": "verified"}
], records
PY

# 5. An unknown zone is an error, not a silent create.
start_server '{"zones": {"other.example": []}}'
if run; then
  echo "expected an unknown zone to fail" >&2
  exit 1
fi

echo "DNS reconciliation creates, retargets, leaves foreign records alone and refuses to guess."
