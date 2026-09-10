#!/usr/bin/env bash
# The node monitoring agent, dry-run on a machine that has none of it.
#
# Two things are being proved. First, that a dry-run changes nothing: no
# exporter, no unit, no textfile directory, no service account. Second, that it
# does not fall over on the shape of failure check mode is prone to - a task
# that asks systemd about a unit an earlier task only *planned* to install, and
# the run stops on "Could not find the requested service".
#
# It needs nothing but a shell: in check mode nothing is downloaded, nothing is
# written and nothing is loaded.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo="$(cd "$root/.." && pwd)"
work="$(mktemp -d)"
out="$work/out"
trap 'rm -rf "$work"' EXIT

for path in /usr/local/bin/node_exporter \
            /usr/local/bin/august-node-sessions \
            /etc/systemd/system/node_exporter.service \
            /etc/systemd/system/august-node-sessions.timer; do
  if [ -e "$path" ]; then
    echo "This test has to run where $path is absent; it is the whole point." >&2
    exit 1
  fi
done

before="$work/before"
after="$work/after"
snapshot() {
  { ls -la /usr/local/bin 2>/dev/null
    ls -la /etc/systemd/system 2>/dev/null
    ls -la /var/lib/node_exporter 2>/dev/null || true
    getent passwd node-exporter || true
  } > "$1"
}

snapshot "$before"

cat > "$work/values.yml" <<'VALUES'
# The firewall belongs to node_base and needs nftables and root; this test is
# about the agent, so the rule is rendered and checked by
# ansible/tests/preflight_guards.yml instead.
node_firewall_enabled: false
node_monitoring_enabled: true
node_monitoring_sessions_enabled: true
monitoring_scrape_cidrs: ["198.51.100.20/32"]
node_public_tcp_ports: [80, 443]
node_public_udp_ports: []
remnawave_node_port: 2222
inbound_specs:
  - tag: TEST_01_REALITY
    port: 443
    network: raw
VALUES

cat > "$work/play.yml" <<'PLAY'
---
- name: Dry-run the node monitoring agent
  hosts: localhost
  connection: local
  gather_facts: true
  become: false
  vars_files:
    - values.yml
  roles:
    - role: node_monitoring
PLAY

ANSIBLE_CONFIG="$repo/ansible.cfg" ANSIBLE_ROLES_PATH="$root/roles" \
  ansible-playbook "$work/play.yml" -i 'localhost,' -c local \
  --check --diff | tee "$out"

grep -Eq 'failed=0([[:space:]]|$)' "$out" || {
  echo "A dry-run of the node monitoring agent must not fail" >&2
  exit 1
}
if grep -Fq 'Could not find the requested service' "$out"; then
  echo "The dry-run asked systemd about a unit it had not installed" >&2
  exit 1
fi

snapshot "$after"
if ! diff -u "$before" "$after" > "$work/drift"; then
  echo "A dry-run changed the machine:" >&2
  cat "$work/drift" >&2
  exit 1
fi

for path in /usr/local/bin/node_exporter \
            /usr/local/bin/august-node-sessions \
            /etc/systemd/system/node_exporter.service; do
  if [ -e "$path" ]; then
    echo "A dry-run created $path" >&2
    exit 1
  fi
done

echo "The node monitoring agent dry-runs cleanly and changes nothing."
