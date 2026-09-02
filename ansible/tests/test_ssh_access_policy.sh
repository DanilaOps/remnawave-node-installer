#!/usr/bin/env bash
# The operator's own SSH access to a node, checked against the real nftables
# parser rather than against a grep of a template.
#
# The failure this exists for: a node's firewall input chain drops by default,
# and TCP/22 is opened only for the addresses in management_cidrs. If the
# operator's own address is not in there, "02 - Install / Reconcile Node"
# installs a firewall that leaves Semaphore as the only way in - and because the
# ruleset is rendered from scratch on every run (the template starts with
# "destroy table"), a rule added by hand on the node disappears at the next
# reconcile. So what has to be proven is that the address from
# management_cidrs_extra survives resolution, rendering, a real load, and a
# second identical run.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo="$(cd "$root/.." && pwd)"
work="$(mktemp -d)"
table="remnawave_ssh_policy_test"

# How much of nftables this machine allows. Parsing needs only the binary;
# loading a table needs CAP_NET_ADMIN. Whichever is available is used, and the
# part that is not possible here is reported as NOT VERIFIED rather than skipped
# silently - the load is the strongest assertion in this file.
nft_cmd=()
if command -v nft >/dev/null 2>&1 && nft list ruleset >/dev/null 2>&1; then
  nft_cmd=(nft)
elif command -v sudo >/dev/null 2>&1 && sudo -n nft list ruleset >/dev/null 2>&1; then
  nft_cmd=(sudo -n nft)
fi

cleanup() {
  if [ "${#nft_cmd[@]}" -gt 0 ]; then
    "${nft_cmd[@]}" delete table inet "$table" >/dev/null 2>&1 || true
  fi
  rm -rf "$work"
}
trap cleanup EXIT

CONTROLLER="198.51.100.20"
OPERATOR_V4="100.64.0.7"
OPERATOR_V6="fd00:2026::7"
PANEL="198.51.100.30"

ANSIBLE_CONFIG="$repo/ansible.cfg" ansible-playbook \
  -i localhost, -c local "$root/tests/render_ssh_policy.yml" \
  -e render_output_dir="$work" > "$work/render.log" 2>&1 || {
    echo "Rendering the SSH access policy failed:" >&2
    tail -40 "$work/render.log" >&2
    exit 1
  }

fail() { echo "$1" >&2; exit 1; }

# --- 1. the controller and the operator are both allowed on 22 ---------------
operator="$work/operator/remnawave-filter.nft"
grep -Fq "ip saddr ${CONTROLLER}/32 tcp dport 22 accept" "$operator" ||
  fail "the controller's own address is not allowed on TCP/22"
grep -Fq "ip saddr ${OPERATOR_V4}/32 tcp dport 22 accept" "$operator" ||
  fail "the operator's address from management_cidrs_extra is not allowed on TCP/22"
[ "$(grep -c 'tcp dport 22 accept' "$operator")" = 2 ] ||
  fail "expected exactly two rules for TCP/22, got $(grep -c 'tcp dport 22 accept' "$operator")"

# The resolution step, not just the template: both addresses have to come out of
# preflight_management_cidrs.yml, which is what a real run uses.
python3 - "$work/operator/management_cidrs.json" "${CONTROLLER}/32" "${OPERATOR_V4}/32" <<'PY'
import json, sys
resolved = json.load(open(sys.argv[1]))
expected = sys.argv[2:]
assert resolved == expected, f"resolution produced {resolved}, expected {expected}"
print(f"resolved management_cidrs: {resolved}")
PY

# --- 2. a second identical run changes nothing -------------------------------
# This is what makes a re-run of template 02 a no-op for the firewall: the
# template task reports ok, so node_base/tasks/firewall.yml skips its whole
# apply block - no rollback timer, no table reload, no dropped connection.
for scenario in operator controller dual-stack; do
  cmp -s "$work/$scenario/remnawave-filter.nft" "$work/$scenario/remnawave-filter.second.nft" ||
    fail "$scenario: a second render of the same inputs differs, so every re-run would reload the firewall"
done

# The step above proves the rendered file is identical. This proves that being
# identical is what makes the run a no-op: the apply block - rollback timer,
# table reload, connection reset - is gated on the template task's changed flag.
# Without this assertion, "the render is stable" would be an argument rather
# than a fact about the code.
python3 - "$root/roles/node_base/tasks/firewall.yml" <<'PY'
import sys
import yaml

tasks = yaml.safe_load(open(sys.argv[1]))
apply_block = [t for t in tasks if t.get("name") == "Apply changed firewall with automatic rollback"]
assert len(apply_block) == 1, "the firewall apply block was renamed or removed"
gate = apply_block[0].get("when")
assert gate == "node_base_firewall_template.changed", (
    f"the apply block is gated on {gate!r}; a re-run is only a no-op while it is gated "
    "on the template task's changed flag"
)
render = [t for t in tasks if t.get("name") == "Render the Remnawave firewall ruleset"]
assert len(render) == 1 and render[0].get("register") == "node_base_firewall_template"
print("the firewall apply block is gated on an unchanged render")
PY

# --- 3. empty management_cidrs_extra keeps the safe policy -------------------
controller_only="$work/controller/remnawave-filter.nft"
grep -Fq "ip saddr ${CONTROLLER}/32 tcp dport 22 accept" "$controller_only" ||
  fail "with no extras the controller must still reach TCP/22"
[ "$(grep -c 'tcp dport 22 accept' "$controller_only")" = 1 ] ||
  fail "with no extras exactly one address may reach TCP/22"
grep -Fq "policy drop" "$controller_only" ||
  fail "the input chain must still drop by default"
grep -Fq "$OPERATOR_V4" "$controller_only" &&
  fail "an address that was not configured appeared in the ruleset"

# --- 4. IPv4 /32 and IPv6 /128 ------------------------------------------------
dual="$work/dual-stack/remnawave-filter.nft"
grep -Fq "ip saddr ${OPERATOR_V4}/32 tcp dport 22 accept" "$dual" ||
  fail "the IPv4 /32 is missing or was rendered as an IPv6 rule"
grep -Fq "ip6 saddr ${OPERATOR_V6}/128 tcp dport 22 accept" "$dual" ||
  fail "the IPv6 /128 is missing or was rendered as an IPv4 rule"
grep -Fq "ip saddr ${OPERATOR_V6}" "$dual" &&
  fail "an IPv6 address was rendered with the IPv4 matcher"

# --- 5. nothing else was opened ----------------------------------------------
for ruleset in "$work"/*/remnawave-filter.nft; do
  grep -Eq "tcp dport \{ 80, 443 \} accept" "$ruleset" ||
    fail "$ruleset: the public port set is not the expected 80 and 443"
  # NODE_PORT stays source-restricted to the panel, and never appears in the
  # unrestricted set.
  grep -Fq "ip saddr ${PANEL}/32 tcp dport 2222 accept" "$ruleset" ||
    fail "$ruleset: the node port is not restricted to the panel"
  python3 - "$ruleset" <<'PY'
import re, sys
text = open(sys.argv[1]).read()
public = re.findall(r"dport \{([^}]*)\} accept", text)
opened = {int(p) for group in public for p in group.replace(" ", "").split(",") if p}
forbidden = opened & {22, 2222, 9100}
assert not forbidden, f"{sorted(forbidden)} appear in an unrestricted port set"
print(f"unrestricted ports: {sorted(opened)}")
PY
done

# --- 6. fail2ban cannot ban the two parties the firewall lets in --------------
# The jail's only reachable sources are the controller and the operator, because
# everything else is dropped before sshd. Without an exemption it can only lock
# out the people who are supposed to have access, for an hour, and no re-run
# clears it.
jail="$work/operator/remnawave-sshd.local"
grep -Eq "^ignoreip = .*127\.0\.0\.1/8" "$jail" || fail "the jail does not exempt loopback"
grep -Fq "${CONTROLLER}/32" "$jail" || fail "the jail can ban the controller"
grep -Fq "${OPERATOR_V4}/32" "$jail" || fail "the jail can ban the operator"
grep -Fq "${OPERATOR_V6}/128" "$work/dual-stack/remnawave-sshd.local" ||
  fail "the jail can ban the operator's IPv6 address"

# --- 6b. a node whose SSH is open to the internet -----------------------------
# The regression this exists for. management_cidrs is the firewall's allow list
# and it may legitimately hold 0.0.0.0/0 and ::/0: a fleet can decide a node
# takes SSH from anywhere. Copying that list into ignoreip hands fail2ban
# "ignore 0.0.0.0/0", which disables the jail for every source - on precisely
# the node that is exposed and, in this fleet, still accepts a root password.
# So the /0 has to be honoured in one file and filtered out of the other.
public_nft="$work/public-ssh/remnawave-filter.nft"
public_jail="$work/public-ssh/remnawave-sshd.local"

# The firewall keeps what the operator asked for.
grep -Fq "ip saddr 0.0.0.0/0 tcp dport 22 accept" "$public_nft" ||
  fail "the firewall dropped the deliberate IPv4 default route on TCP/22"
grep -Fq "ip6 saddr ::/0 tcp dport 22 accept" "$public_nft" ||
  fail "the firewall dropped the deliberate IPv6 default route on TCP/22"
grep -Fq "ip saddr ${OPERATOR_V4}/32 tcp dport 22 accept" "$public_nft" ||
  fail "the operator's own address is missing from the ruleset"

# The jail exempts the named hosts and nothing that means "everyone". Every
# assertion below reads the ignoreip directive and not the file: the comment
# above it explains the /0 rule and therefore contains the very strings a
# whole-file grep would trip over.
ignore_line="$(grep '^ignoreip = ' "$public_jail")" ||
  fail "the jail has no ignoreip directive"
case "$ignore_line" in
  *127.0.0.1/8*) ;;
  *) fail "the jail does not exempt loopback" ;;
esac
case "$ignore_line" in
  *"${CONTROLLER}/32"*) ;;
  *) fail "the jail can ban the controller on a public-SSH node" ;;
esac
case "$ignore_line" in
  *"${OPERATOR_V4}/32"*) ;;
  *) fail "the jail can ban the operator on a public-SSH node" ;;
esac
case "$ignore_line" in
  *0.0.0.0/0*) fail "ignoreip contains 0.0.0.0/0, which switches the sshd jail off entirely" ;;
esac
case "$ignore_line" in
  *::/0*) fail "ignoreip contains ::/0, which switches the sshd jail off for IPv6" ;;
esac

# Not just those two spellings: no entry on the line may carry a zero prefix.
python3 - "$public_jail" <<'PY'
import ipaddress, sys

line = next(
    (l for l in open(sys.argv[1]) if l.startswith("ignoreip = ")),
    None,
)
assert line is not None, "the jail has no ignoreip line"
entries = line.split("=", 1)[1].split()
zero = []
for entry in entries:
    try:
        network = ipaddress.ip_network(entry, strict=False)
    except ValueError:
        continue
    if network.prefixlen == 0:
        zero.append(entry)
assert not zero, f"ignoreip exempts everyone through {zero}"
assert any(e.endswith("/32") or e.endswith("/128") for e in entries), (
    "ignoreip kept no specific host at all; the filter is too aggressive"
)
print(f"ignoreip on a public-SSH node: {entries}")
PY

# --- 7. the real nftables parser, and a real double load ---------------------
# A rendered ruleset that nft rejects would fail on the node with the rollback
# timer already armed, so it is parsed here by nft itself.
live_verified=0
if [ "${#nft_cmd[@]}" -gt 0 ]; then
  for ruleset in "$work"/*/remnawave-filter.nft; do
    "${nft_cmd[@]}" -c -f "$ruleset" || fail "$ruleset: nft rejected the rendered ruleset"
  done

  # Loading the same ruleset twice must leave the same table. This is the
  # machine level of assertion 2: "destroy table" then create is what makes the
  # reconcile converge instead of accumulating rules.
  "${nft_cmd[@]}" -f "$operator"
  "${nft_cmd[@]}" list table inet "$table" > "$work/live-first.nft"
  "${nft_cmd[@]}" -f "$operator"
  "${nft_cmd[@]}" list table inet "$table" > "$work/live-second.nft"
  cmp -s "$work/live-first.nft" "$work/live-second.nft" ||
    fail "loading the ruleset twice produced a different table: $(diff -u "$work/live-first.nft" "$work/live-second.nft" | head -20)"

  # And the live table really does allow both. nft prints a /32 host route
  # without its prefix, which is why the project has a filter for this
  # comparison; here the two spellings are accepted the same way.
  python3 - "$work/live-first.nft" "$CONTROLLER" "$OPERATOR_V4" <<'PY'
import re, sys
text = open(sys.argv[1]).read()
for address in sys.argv[2:]:
    pattern = rf"(^|[\s,{{]){re.escape(address)}(/32)?($|[\s,}}])"
    assert re.search(pattern, text), f"{address} is not in the live ruleset"
assert "dport 22 accept" in text, "the live table has no rule for TCP/22"
print("live table allows the controller and the operator on TCP/22")
PY
  "${nft_cmd[@]}" delete table inet "$table"
  live_verified=1
else
  echo "NOT VERIFIED: nftables cannot be loaded on this machine (no binary or no" >&2
  echo "              CAP_NET_ADMIN), so the rendered rulesets were checked as text only." >&2
fi

# --- 8. the permission is permanent, not a Semaphore survey ------------------
# A survey value lives for one job. If a CIDR were collected that way, the next
# reconcile would render the ruleset without it and close the operator out.
python3 - "$root/tools/semaphore_bootstrap.py" <<'PY'
import re, sys
source = open(sys.argv[1]).read()
names = set(re.findall(r'"name":\s*"([a-z_0-9]+)"', source))
survey_only = {"bootstrap_ssh_password", "bootstrap_trust_new_host_keys"}
leaked = {n for n in names if "cidr" in n or "management" in n}
assert not leaked, f"a Semaphore survey collects {sorted(leaked)}; it must live in fleet.yml"
assert survey_only <= names, "the expected survey fields are gone; re-check this assertion"
print(f"survey fields: {sorted(survey_only)}")
PY

if [ "$live_verified" = 1 ]; then
  echo "The operator's address survives resolution, rendering, a real nft load and a re-run."
else
  echo "The operator's address survives resolution, rendering and a re-run (nft load NOT VERIFIED here)."
fi
