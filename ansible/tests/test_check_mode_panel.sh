#!/usr/bin/env bash
# Proves --check --diff really works against the Panel API, in the three states
# that matter: a deployment that does not exist yet, the apply itself, and a
# dry-run of an already-reconciled deployment. The last one must report no
# change at all - a dry-run that claims work on a converged fleet is worthless.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo="$(cd "$root/.." && pwd)"
state="$(mktemp)"
first="$(mktemp)"
apply="$(mktemp)"
again="$(mktemp)"
# The role hardens the directory of the identity file to 0700 and one owner, so
# the fixture gives it a directory of its own inside a workspace this test
# created. A bare mktemp file would make that directory /tmp itself.
node_work="$(mktemp -d)"
node_env="$node_work/identity/.env"
cleanup() {
  kill "${server_pid:-}" 2>/dev/null || true
  rm -rf "$state" "$first" "$apply" "$again" "$node_work"
}
trap cleanup EXIT

python "$root/tests/seed_shared_profile.py" "$state"
python "$root/tests/mock_panel.py" --state "$state" &
server_pid=$!

for _ in $(seq 1 30); do
  curl --silent --fail http://127.0.0.1:18080/api/nodes >/dev/null && break
  sleep 0.1
done

run() {
  ANSIBLE_CONFIG="$repo/ansible.cfg" ansible-playbook \
    -i localhost, -c local "$root/tests/panel_idempotency.yml" \
    -e test_node_env_path="$node_env" \
    -e "remnawave_node_identity_owner=$(id -un)" \
    -e "remnawave_node_identity_group=$(id -gn)" "$@"
}

# 1. Dry-run before anything exists. Nothing may fail: the objects this run would
#    create legitimately have no UUID yet, and the role has to say so instead of
#    dereferencing an empty result.
run --check --diff | tee "$first"
grep -Eq 'failed=0([[:space:]]|$)' "$first" || {
  echo "A dry-run of a deployment that does not exist yet must not fail" >&2
  exit 1
}
for expected in \
  "Would create Node" \
  "Would create Internal Squad" \
  "Dry-run: nothing was written"; do
  grep -Fq "$expected" "$first" || {
    echo "A dry-run has to report what it would do; missing: $expected" >&2
    exit 1
  }
done

# 2. The apply.
run | tee "$apply"
grep -Eq 'failed=0([[:space:]]|$)' "$apply" || { echo "Apply failed" >&2; exit 1; }

# 3. Dry-run of the reconciled deployment: every read now returns real state, so
#    every check really executes and nothing may be reported as changing.
run --check --diff | tee "$again"
grep -Eq 'failed=0([[:space:]]|$)' "$again" || {
  echo "A dry-run of a reconciled deployment must not fail" >&2
  exit 1
}
grep -Eq 'changed=0([[:space:]]|$)' "$again" || {
  echo "A dry-run of a reconciled deployment reported changes" >&2
  exit 1
}
if grep -Fq "Would create" "$again"; then
  echo "A dry-run of a reconciled deployment proposed creating something" >&2
  exit 1
fi

echo "Dry-runs are honest before, during and after reconciliation."
