#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo="$(cd "$root/.." && pwd)"
state="$(mktemp)"
output="$(mktemp)"
# The role hardens the directory of the identity file to 0700 and one owner, so
# the fixture gives it a directory of its own inside a workspace this test
# created. A bare mktemp file would make that directory /tmp itself.
node_work="$(mktemp -d)"
node_env="$node_work/identity/.env"
rm -f "$state"
cleanup() {
  kill "${server_pid:-}" 2>/dev/null || true
  rm -rf "$state" "$output" "$node_work"
}
trap cleanup EXIT

python "$root/tests/mock_panel.py" --port 18081 --state "$state" &
server_pid=$!
for _ in $(seq 1 30); do
  curl --silent --fail http://127.0.0.1:18081/api/nodes >/dev/null && break
  sleep 0.1
done

expect_failure() {
  local label="$1"
  shift
  if ANSIBLE_CONFIG="$repo/ansible.cfg" ansible-playbook \
    -i localhost, -c local "$root/tests/panel_idempotency.yml" \
    -e remnawave_panel_url=http://127.0.0.1:18081 \
    -e test_node_env_path="$node_env" \
    -e "remnawave_node_identity_owner=$(id -un)" \
    -e "remnawave_node_identity_group=$(id -gn)" "$@" >"$output" 2>&1; then
    echo "Expected $label deployment to fail" >&2
    cat "$output" >&2
    exit 1
  fi
}

expect_failure HTTP-401 -e remnawave_panel_token=mock-401 -e remnawave_api_retries=1
expect_failure HTTP-403 -e remnawave_panel_token=mock-403 -e remnawave_api_retries=1
expect_failure HTTP-500 -e remnawave_panel_token=mock-500 -e remnawave_api_retries=1
expect_failure timeout -e remnawave_panel_token=mock-timeout -e remnawave_api_timeout=1 -e remnawave_api_retries=1
# config_profile_create lets this case reach POST /config-profiles, where the mock
# injects the conflict; without it the run stops earlier on the missing shared profile.
expect_failure HTTP-409 -e profile_name=CONFLICT -e config_profile_create=true
expect_failure missing-config-profile -e profile_name=DOES-NOT-EXIST

echo "Panel role rejects HTTP 401/403/409/500, timeouts and a missing Config Profile."
