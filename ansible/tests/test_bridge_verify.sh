#!/usr/bin/env bash
# tr01 finished green end to end and then failed acceptance on a field it never
# declared: bridge.yml was imported statically, and Ansible templates a task's
# delegate_to before it evaluates the inherited condition, so a direct node's
# bridge_spec of {enabled: false} crashed on entry_inventory_host. This runs the
# role's real bridge.yml in the three shapes bridge_spec has - direct node,
# managed entry node, probe-only - and requires the direct node to skip it
# without touching a single bridge-only field.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo="$(cd "$root/.." && pwd)"
port=18475
out="$(mktemp)"
cleanup() {
  kill "${listener_pid:-}" 2>/dev/null || true
  rm -f "$out"
}
trap cleanup EXIT

# The entry-node scenario checks the bridge port for real, so something has to
# listen on it.
python3 -c "
import socketserver, http.server
socketserver.TCPServer(('127.0.0.1', $port), http.server.BaseHTTPRequestHandler).serve_forever()
" &
listener_pid=$!
for _ in $(seq 1 30); do
  python3 -c "import socket; socket.create_connection(('127.0.0.1', $port), 1).close()" 2>/dev/null && break
  sleep 0.1
done

ANSIBLE_CONFIG="$repo/ansible.cfg" ansible-playbook \
  -i localhost, -c local "$root/tests/bridge_verify.yml" \
  -e test_bridge_port="$port" | tee "$out"

grep -qF 'bridge verification was skipped without templating bridge-only fields' "$out" || {
  echo "the direct-node play did not reach its final task" >&2
  exit 1
}
grep -qE 'failed=0' "$out" || { echo "bridge acceptance failed" >&2; exit 1; }
if grep -qE 'failed=[1-9]|unreachable=[1-9]' "$out"; then
  echo "bridge acceptance failed or delegated to a host that does not exist" >&2
  exit 1
fi
echo "Direct nodes skip bridge acceptance safely; bridge nodes still verify it."
