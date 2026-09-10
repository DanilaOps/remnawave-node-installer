#!/usr/bin/env bash
# The controller role's firewall chain, dry-run on a machine that does not have
# its systemd units. This is the exact shape of the failure that got past the
# first check-mode audit: the unit file is only *planned*, Ansible reports the
# template as changed, and the next task asks systemd to arm a timer that does
# not exist. The run stopped on "Could not find the requested service".
#
# It needs nothing but systemd and nftables: in check mode nothing is written and
# nothing is loaded.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo="$(cd "$root/.." && pwd)"
values="$(mktemp)"
out="$(mktemp)"
trap 'rm -f "$values" "$out"' EXIT

for unit in remnawave-controller-firewall.service \
            remnawave-controller-firewall-rollback.service \
            remnawave-controller-firewall-rollback.timer; do
  if [ -f "/etc/systemd/system/$unit" ]; then
    echo "This test has to run where $unit is absent; it is the whole point." >&2
    exit 1
  fi
done

# This test is about the firewall chain only, so it points the role at a
# Semaphore configuration that does not exist: whatever Semaphore the machine
# running the test happens to have is none of this test's business.
cat > "$values" <<VALUES
semaphore_config_path: ${out}.d/semaphore-config.json
VALUES
cat >> "$values" <<'VALUES'
controller_firewall_enabled: true
controller_ssh_allowed_cidrs: ["192.0.2.7/32"]
controller_proxy_enabled: false
controller_backup_enabled: false
controller_swap_enabled: false
VALUES

ANSIBLE_CONFIG="$repo/ansible.cfg" ansible-playbook \
  "$root/playbooks/controller.yml" -i 'localhost,' -c local \
  -e controller_group=localhost -e "@$values" \
  --check --diff --tags firewall | tee "$out"

grep -Eq 'failed=0([[:space:]]|$)' "$out" || {
  echo "A dry-run of the controller firewall must not fail" >&2
  exit 1
}
grep -Fq 'Could not find the requested service' "$out" && {
  echo "The dry-run asked systemd about a unit it had not installed" >&2
  exit 1
}
grep -Fq 'Would reload systemd, arm remnawave-controller-firewall-rollback.timer' "$out" || {
  echo "The dry-run has to say what it would do to the live firewall instead" >&2
  exit 1
}
grep -Fq 'Would install and enable remnawave-controller-firewall.service' "$out" || {
  echo "The dry-run has to say that the persistent firewall unit would be installed" >&2
  exit 1
}

echo "The controller firewall dry-run is honest on a host without its units."
