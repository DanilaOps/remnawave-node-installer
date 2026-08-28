#!/usr/bin/env bash
# Fleet configuration must load from ansible/playbooks/group_vars whatever the
# inventory is. Semaphore keeps its own inventory outside the repository, so this
# test builds the smallest possible external inventory - one host, one address,
# no group_vars anywhere near it - and requires the run to still resolve the
# whole node definition.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
work="$(mktemp -d)"
cleanup() { rm -rf "$work"; }
trap cleanup EXIT

cat > "$work/hosts.yml" <<INVENTORY
all:
  children:
    remnawave_nodes:
      hosts:
        ee01:
          ansible_host: 203.0.113.10
INVENTORY

cd "$repo"
output="$work/out.txt"
ansible-playbook -i "$work/hosts.yml" ansible/playbooks/show_node_identity.yml | tee "$output"

for expected in \
  "node_id          = ee_01" \
  "node_name        = EE-01" \
  "selfsteal_domain = ee01.example.com" \
  "inbound tag      = EE_01_REALITY" \
  "ansible_user     = deployer"; do
  grep -qF "$expected" "$output" || {
    echo "external inventory lost fleet configuration: expected '$expected'" >&2
    exit 1
  }
done

grep -qE 'failed=0' "$output" || { echo "assertions failed" >&2; exit 1; }
echo "External inventory resolves the full node definition from playbooks/group_vars."
