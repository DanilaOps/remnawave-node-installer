#!/usr/bin/env bash
# Semaphore's "01 - Preflight" template on a VPS that has just been created and
# does not carry the managed account yet.
#
# This is the shape of the live failure it exists for: bootstrap's own tasks were
# correctly skipped by --tags preflight, but the play that verifies the managed
# account was declared gather_facts: true, and the implicit "Gathering Facts"
# task carries the "always" tag. Preflight therefore opened an SSH session as
# deployer before deployer existed and the run died with
# "deployer@<node>: Permission denied (publickey,password)" - before
# install_node.yml's controller-side play had run a single check.
#
# The node here is a dead address (127.0.0.1:1) and, more importantly, ssh,
# scp, sftp, sshpass and ssh-keyscan are replaced by stubs that record every
# invocation and refuse to connect. A preflight run has to finish without a
# single entry in that log: that, and not the absence of an error message, is
# what "controller-side only" means.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo="$(cd "$root/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

mkdir -p "$work/bin"
for tool in ssh scp sftp sshpass; do
  cat > "$work/bin/$tool" <<STUB
#!/bin/sh
printf '$tool %s\n' "\$*" >> "\$REMNAWAVE_SSH_ATTEMPT_LOG"
exit 255
STUB
done
cat > "$work/bin/ssh-keyscan" <<'STUB'
#!/bin/sh
printf 'ssh-keyscan %s\n' "$*" >> "$REMNAWAVE_SSH_ATTEMPT_LOG"
exit 1
STUB
chmod +x "$work/bin"/*

# A real inventory host, named the way the fleet names them, so the identity
# under test is the derived one rather than something this test invented. The
# address is loopback on a port nothing listens on, so even a stub-free
# environment fails immediately instead of waiting out a timeout.
cat > "$work/hosts.yml" <<'INVENTORY'
---
all:
  children:
    remnawave_nodes:
      hosts:
        ee01:
          ansible_host: 127.0.0.1
          ansible_port: 1
INVENTORY

# Deployment values, so preflight does not stop on the repository's
# documentation values. The panel and DNS are switched off and the omission is
# acknowledged: this test is about what preflight touches, not about what it
# finds in a panel.
cat > "$work/fleet.yml" <<'VALUES'
---
remnawave_panel_url: https://panel.clean-vps.test
node_domain_zone: clean-vps.test
acme_email: ops@clean-vps.test
remnawave_panel_token: preflight-test-token
remnawave_panel_cidrs: ["198.51.100.20/32"]
internal_squad_name: Default
internal_squad_uuid: ""
preflight_check_panel: false
preflight_ack_unchecked_panel: true
preflight_check_dns: false
VALUES

preflight_run() {
  # 01 - Preflight, exactly as the Semaphore template runs it.
  env PATH="$work/bin:$PATH" \
      ANSIBLE_CONFIG="$repo/ansible.cfg" \
      REMNAWAVE_SSH_ATTEMPT_LOG="$1" \
    ansible-playbook "$root/playbooks/provision_node.yml" \
      -i "$work/hosts.yml" --limit ee01 -e "@$work/fleet.yml" \
      --tags preflight --check "${@:2}"
}

# --- 1. the real thing --------------------------------------------------------
: > "$work/attempts.log"
preflight_run "$work/attempts.log" > "$work/out" 2>&1 || {
  echo "01 - Preflight failed on a VPS without the managed account:" >&2
  tail -30 "$work/out" >&2
  exit 1
}

if [ -s "$work/attempts.log" ]; then
  echo "Preflight reached for the node. It must stay on the controller:" >&2
  cat "$work/attempts.log" >&2
  exit 1
fi

grep -Eq 'unreachable=0([[:space:]]|$)' "$work/out" || {
  echo "Preflight reported an unreachable host" >&2; tail -20 "$work/out" >&2; exit 1
}
grep -Eq 'failed=0([[:space:]]|$)' "$work/out" || {
  echo "Preflight reported a failed task" >&2; tail -20 "$work/out" >&2; exit 1
}
grep -Fq 'Permission denied' "$work/out" && {
  echo "Preflight still tried to authenticate against the node" >&2; exit 1
}
grep -Fq 'Gathering Facts' "$work/out" && {
  echo "An implicit fact gathering task is back in the preflight path" >&2; exit 1
}
# Not a run that passed by doing nothing: controller-side preflight has to have
# executed its first assertion.
grep -Fq 'Validate the identity derived from the inventory' "$work/out" || {
  echo "Controller-side preflight did not run, so this run proved nothing" >&2
  tail -30 "$work/out" >&2
  exit 1
}

# --- 2. the same run with the guard switched off ------------------------------
# Without it the stubs must catch a connection attempt. This keeps the test
# honest: an empty attempt log in step 1 has to mean "nothing connected", not
# "the stubs were never in the way".
: > "$work/control.log"
preflight_run "$work/control.log" -e remnawave_preflight_only=false \
  > "$work/control.out" 2>&1 || true
if [ ! -s "$work/control.log" ]; then
  echo "With the guard disabled nothing tried to connect either - this test" >&2
  echo "cannot tell a fixed preflight from a broken harness." >&2
  exit 1
fi
grep -Fq 'deployer' "$work/control.log" || {
  echo "The control run connected as somebody other than the managed account" >&2
  cat "$work/control.log" >&2
  exit 1
}

# --- 3. the runs that do need facts ------------------------------------------
# Moving fact gathering into an explicit task must not leave the other templates
# without facts: node-side preflight reads ansible_env, node_base reads the
# distribution and the architecture.
list_tasks() {
  # Into a file rather than a pipe: ansible-playbook refuses to start when its
  # standard streams are not blocking, which a command substitution can produce.
  env ANSIBLE_CONFIG="$repo/ansible.cfg" ansible-playbook \
    "$root/playbooks/provision_node.yml" -i "$work/hosts.yml" --limit ee01 \
    -e "@$work/fleet.yml" --list-tasks "${@:2}" > "$1" 2>&1
}

list_tasks "$work/tasks-verify" --tags node_verify
grep -Fq 'Gather facts from the node' "$work/tasks-verify" || {
  echo "03 - Verify Node would run without facts" >&2; exit 1
}
list_tasks "$work/tasks-full"
grep -Fq 'Gather facts from the node' "$work/tasks-full" || {
  echo "A full run would install the node without facts" >&2; exit 1
}
grep -Fq 'Gather facts through the managed account' "$work/tasks-full" || {
  echo "A full run would verify the managed account without facts" >&2; exit 1
}

echo "01 - Preflight stays on the controller on a VPS that has no managed account."
