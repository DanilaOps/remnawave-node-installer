#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
state="$(mktemp)"
output="$(mktemp)"
node_env="$(mktemp)"
rm -f "$state"
rm -f "$node_env"

cleanup() {
  kill "${server_pid:-}" 2>/dev/null || true
  rm -f "$state" "$output" "$node_env"
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
  if ANSIBLE_CONFIG="$root/ansible.cfg" ansible-playbook \
    -i localhost, -c local "$root/tests/panel_idempotency.yml" \
    -e remnawave_panel_url=http://127.0.0.1:18081 \
    -e test_node_env_path="$node_env" "$@" >"$output" 2>&1; then
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
expect_failure missing-shared-profile -e profile_name=DOES-NOT-EXIST

echo "Panel role rejects HTTP 401/403/409/500, timeouts and a missing shared profile."
