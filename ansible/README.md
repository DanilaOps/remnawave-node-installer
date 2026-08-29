# Ansible deployment for Remnawave nodes

This directory is the non-interactive replacement for `remnawave-node.sh`. It does not
create a VPS and does not diagnose TSPU blocking. Everything after the server exists —
DNS, the operating system, the Remnawave Panel resources, the node runtime, the masking
site and the acceptance checks — is owned here.

## One command per node

Every command in this project runs from the **repository root** against the one
`ansible.cfg` there — the wrappers, the tests and Semaphore all use the same
paths, so there is nothing to remember and nothing to keep in step.

Once, on the machine that will run Ansible:

```bash
./setup-controller
```

It creates the virtualenv, installs the pinned dependencies and the collections
(into the checkout, so every account that runs from here sees the same versions),
generates a dedicated deployer SSH key if there is none, stores the vault
password outside the repository so later runs stop asking, and writes the
deployment values and the encrypted vault from what you answer. It is
idempotent, so run it again after pulling changes; it never touches a node.

Then, for every node, forever:

```yaml
# ansible/inventories/production/hosts.yml
ee01:
  ansible_host: 203.0.113.10
```

```bash
./provision-node ee01 --first-run
```

That is the whole flow: buy the VPS, point `ee01.<zone>` at it, add one line, run
one command. `--first-run` is only for a server created minutes ago: it accepts
the SSH host key presented on first contact. Later runs are just
`./provision-node ee01`, and a host key that is already known and has changed is
always a failure, never a new trust decision. The wrapper only prepares the local environment — virtualenv,
vault password, and a one-off root password prompt when the server has not been
bootstrapped yet — then runs `ansible/playbooks/provision_node.yml`. Running that
playbook directly does exactly the same thing, and every flag passes through:

```bash
./provision-node ee01 --check
./provision-node ee01 --tags preflight --check
./provision-node ee01 --tags certificate -v
```

The root password is read into the process environment and never written to
disk, never placed in the command line and never needed again: bootstrap
installs the controller's key on the managed account, and the next run of the
same command finds that account already working and skips bootstrap entirely.
For unattended runs the password may instead live in the vault as
`vault_node_root_password`.

Everything else about a node is derived from the inventory hostname:

| Derived value | From |
|---|---|
| `node_id` (`ee_01`) | inventory hostname |
| `node_name` (`EE-01`) | inventory hostname |
| `node_country` (`EE`) | leading letters of the hostname |
| `node_host_remark` (`🇪🇪 Estonia`) | `node_country` via `group_vars/all/countries.yml` |
| `node_public_ip` | `ansible_host` |
| `selfsteal_domain` (`ee01.<zone>`) | hostname plus `node_domain_zone` |
| `inbound_specs[0].tag` (`EE_01_REALITY`) | `node_name` |
| `host_specs[0]` | `node_name`, `selfsteal_domain`, `node_country` |
| `selfsteal_virtual_hosts` | `selfsteal_domain` |
| the entire decoy site | `node_id` |
| `management_cidrs` | the live SSH connection (see below) |

A host that does not follow the `ee01` pattern declares what it needs — usually
just `selfsteal_domain` — in its own block; preflight rejects a hostname it
cannot parse rather than guessing. Adding a country is one line in
`ansible/playbooks/group_vars/all/countries.yml`; a code that is missing from it
stops the run with that file's path in the message rather than publishing a Host
with a blank label.

To see what a line of inventory expands to, without a server and without
secrets:

```bash
ansible-playbook ansible/playbooks/show_node_identity.yml --limit ee01
```

`ansible/tests/test_external_inventory.sh` runs that against a throwaway
inventory that contains nothing but a hostname and an address, which is what
Semaphore provides — the fleet configuration has to come from the playbook's own
`group_vars`, not from next to an inventory.

## Checks that run before anything changes

The operator is not asked to verify anything by hand, and the checks are split by
what they need, so each one runs at the only point where it can be both truthful
and harmless.

**Before anything anywhere changes** — its own play, with no connection to the
node at all, so it works even before the VPS exists: the inventory hostname
parses into a usable identity and the country has a display name; every
configured CIDR is valid; the panel answers and the token has scope; the profile
list is not suspiciously empty; the node's Config Profile exists and carries
routing rules; no other profile already owns this node's inbound tag; every
inbound in that profile is one the panel strips per node; the panel holds
no conflicting Node for this name or address and no ambiguous Host. The DNS state
is **reported** here, never changed — and if `dns_provider` is `none`, a record
that does not already point at the node fails the run, because nothing later will
fix it.

**After DNS and before the first change to the server:** the platform is Debian
12/13 or Ubuntu 24.04 on a supported architecture; the root filesystem has room;
NTP is synchronised; `selfsteal_domain` resolves to this node's address **from
the node itself**; ports 80, 443 and `NODE_PORT` are free on a fresh install; and
the address sshd reports for the controller is inside the management allow list.

That last one no longer has to be maintained: the controller's own address is
taken from the live SSH connection, so a controller with a changing address
cannot lock itself out. Addresses that must keep SSH access even when nobody is
running from them — a workstation, a jump or rescue host — go in
`management_cidrs_extra`, in the git-ignored override rather than in this
repository. Setting `management_cidrs` explicitly opts out of the discovery and
is still validated against what sshd reports. Every branch of that resolution is
covered by `ansible/tests/management_cidrs.yml`, including the one that matters:
an explicit list that would lock the controller out is refused, not applied.

## What a dry-run really tells you

`--check --diff` is meant to be pressed before a real run, so it is built to be
honest rather than merely quiet. Reads run for real in a dry-run: the panel is
queried, the Config Profile and its inbound UUIDs are resolved, DNS is resolved
from the node, `sshd -T`, `ss` and `nft` are read, the containers are inspected
and the public selfsteal page is fetched. Writes are not made, and nothing that
depends on a write is reported as proven.

That gives three honest outcomes rather than one:

* **A node the panel already carries.** The dry-run walks the whole run and is a
  full health check: it says which Hosts, inbounds and Node fields it would
  change, and every acceptance check that does not depend on this run's own
  writes really executes.
* **A node that exists but whose firewall ruleset or certificate this run would
  rewrite.** Those two acceptance checks stand down — verifying live state
  against a change the dry-run was not allowed to apply would report a failure
  the same run is about to fix — and say so in the output. Everything else is
  still asserted.
* **A node the panel has never seen.** The run stops at the end of the panel
  play with one message naming what is missing (the Node, the Internal Squad,
  the inbound, the Hosts) instead of rendering the node's configuration against
  identifiers the panel has not issued. Everything before that point — the
  token, the Config Profile and its routing, the ownership of every inbound tag,
  DNS, the platform, the SSH policy, the firewall inputs — was checked for real.

The end-to-end tunnel probe is never simulated: it is announced as not executed,
because a probe that reports success without carrying a packet is worse than no
probe.

The same rule covers systemd, where it is easy to get wrong: a dry-run reports a
unit file or a package as `changed` without putting anything on disk, so a task
that then starts, enables or restarts that unit is asking systemd about
something that does not exist, and the run stops on *Could not find the
requested service*. Every role that touches systemd therefore reads
`systemctl list-unit-files` once, for real, and each systemd task says how it
knows its unit is there — either it consults that list, or it is not part of a
dry-run at all and reports what it would have done. Handlers follow the same
rule: a planned change still announces the restart it would cause. The firewall
chain is the strictest case, because arming a rollback timer and swapping the
live nftables table can lock an operator out, so a dry-run performs none of it
and prints the whole sequence instead.

Three tests keep this true. `ansible/tests/test_check_mode.py` fails the build on
both shapes of the bug: a task registering a result from a module Ansible skips
in check mode that a later task reads without a guard, and a systemd task acting
on a unit without saying how a dry-run knows it exists.
`ansible/tests/test_check_mode_panel.sh` runs a dry-run against the mock panel
before, during and after reconciliation and requires the last one to report no
change at all. `ansible/tests/test_check_mode_controller.sh` runs the controller
firewall chain in check mode on a machine that does not have its units, which is
where *Could not find the requested service* came from.

## Roles and execution order

`install_node.yml` contains no deployment tasks. It is three plays, in the only
order that is safe:

1. **controller-side preflight** — reads the inventory and the panel, changes
   nothing anywhere. This is also the Semaphore *Preflight* template.
2. **DNS** — the record has to exist and resolve before ACME and before the node
   resolves its own selfsteal name.
3. **the node** — node-side preflight, then installation and verification.

One node per run: play 3 writes this node's inbound into the profile the whole
fleet shares, and that write is a read-modify-write. A run that resolves to more
than one host is refused unless `node_allow_bulk=true` says so on purpose, and
even then both plays run `serial: 1`.

`dns` reconciles the node's A record at the registrar from the controller, then waits
until public resolvers agree. `node_base` validates the host,
installs base packages and Docker, then applies a rollback-protected nftables policy.
`remnawave_panel` reconciles the node's Config Profile, this node's inbound, the Node, its
Hosts, the Internal Squad and the optional bridge identity through the Remnawave 3.x API.
`remnawave_node` installs the pinned Xray binary, the certificate, the generated masking
site, RemnaNode, optional geodata/plugins and the maintenance units. `node_verify` changes
nothing and confirms the real containers, listeners, certificate, firewall, published
page, Panel links and optional bridge/tunnel probes.

`node_bootstrap` is the sixth role and belongs to `bootstrap.yml` only. It is separate
because it is the one role that runs before the managed account exists: it connects as
root, may find no Python on the target and never escalates. It creates the account with
the controller's public key and password-less sudo — and nothing else. sshd policy has
exactly one owner, `node_base`, which writes the highest-priority drop-in and then
verifies the effective policy with `sshd -T` — the drop-in is `00-remnawave.conf`
because sshd takes the first value it sees and a vendor or cloud-init file
sorting earlier would otherwise win silently.

Small components are task files inside those roles, not additional roles.

## The generated masking site

The node serves a real-looking site in front of Reality. Two failure modes matter and pull
in opposite directions: an identical page on every node links the whole fleet with a single
scan (page hash, DOM shape, class names, favicon, asset names, response size), while a page
that changes on every run destroys idempotency and produces pointless churn.

The site is therefore a pure function of a stable per-node seed. The same node keeps a
byte-identical site forever; different nodes get a different brand, tagline, body copy,
section selection and order, layout, palette, font stack, CSS class names, asset file
names, favicon shape, security-header set and response size.

```yaml
selfsteal_decoy_seed: "{{ node_id }}"   # stable identity, not a timestamp
selfsteal_decoy_salt: ""                # reroll one node, e.g. after it was blocked
selfsteal_decoy_generation: 1           # bump to reroll the whole fleet
```

Values come from SHA-256 of `seed | field-label`, deliberately not from one
`random.Random` stream. A stream's output is an interpreter implementation detail, and
drawing fields in sequence means adding one new field shifts every value after it — which
would re-randomise every node's site on an unrelated template change. Labelled hashing
keeps existing fields stable when a new one is added.

Assets are written under names derived from the same seed, and files left over from a
previous seed are removed from the document root so an old page cannot be fetched by its
old asset name. The page loads nothing from third parties.

`node_verify` fetches the public page and requires it to carry this node's brand, class
names and asset names — a shared default page fails the run.

## DNS

```yaml
dns_provider: regru        # or none, when the record is managed elsewhere
```

The role owns exactly one record per node — the selfsteal A record — and touches nothing
else in the zone. It reads the zone, compares, and then creates the record if it is
missing or retargets it if it points elsewhere; a correct record produces no change. A
second A record for the same name is reported rather than guessed at, and only
`dns_prune_extra_records=true` allows removing the ones that do not match. Records
belonging to other names or other types are never modified.

After a change the run waits until every resolver in `dns_wait_resolvers` returns the
managed address, because ACME and the node's own preflight both resolve the name.

REG.RU specifics: the API is `POST https://api.reg.ru/api/regru2/<method>` with a JSON
document in the `input_data` form field, using `zone/get_resource_records`,
`zone/add_alias` and `zone/remove_record`. Credentials are an account username and
password (or an API-specific password) and come from the vault as `vault_regru_username`
and `vault_regru_password`. REG.RU additionally requires API access to be enabled for the
account and the controller's public address to be allow-listed — the same address that
belongs in `management_cidrs`.

Provider logic lives in `roles/dns/tasks/providers/`; the decision (create, retarget,
leave alone) is provider-independent and unit-tested. Adding Cloudflare later means one
more file there, not a change to the node installation.

## Node names are allocated once

A node is `<COUNTRY>-NN`: `TR-01`, `TR-02`, `NL-01`. The number is decided **once**,
by asking the panel what that country already uses:

```
$ ./provision-node --next TR
country            = TR (Turkey)
TR numbers in use  = 1, 2
next node name     = TR-03
inventory hostname = tr03
config profile     = TR-03   (created by the install, same name)
dns hostname       = tr03.august-vpn.com
```

That command reads the panel and changes nothing. What it prints goes into the
inventory as one line — `tr03: {ansible_host: …}` — and from then on the hostname
*is* the identity: node name `TR-03`, Config Profile `TR-03`, inbound tag
`TR_03_REALITY`, domain `tr03.<zone>`. Nothing else has to be typed, and
`profile_name` is not something an operator maintains.

Allocation is deliberately a separate step rather than something the install does
on the fly. A run that worked out "the next free number" every time would move the
identity of a node that already exists — install `tr01` twice and the second run
would see `TR-01`, decide `TR-02` was next and publish the same machine again
under a second identity. Because the number comes from the inventory hostname, a
re-run of `tr01` can only ever reconcile `TR-01`; no reconcile path is allowed to
call the allocator, and a test enforces that.

Two more consequences worth knowing:

* **A freed number is never handed out again.** With `TR-01` and `TR-03` in use,
  the next is `TR-04`, not the gap at `TR-02`: a number that was live once still
  appears in DNS records, certificates, logs and other people's notes.
* **A number is taken if any object still holds it** — Node, Config Profile or
  Host. A profile left behind by a deleted node keeps its number reserved.

Two installs of the same country started at the same instant could still read the
same "next" value; provisioning is serialised (`serial: 1`, one node per run) and
preflight refuses a Node whose name and address disagree, so the second one stops
instead of attaching to the wrong machine.

## Three panel objects that are easy to confuse

Remnawave has three separate things, two of which the UI describes with the words "Xray
JSON". Feeding the name of one to the lookup of another produces a run that fails
complaining about the wrong object entirely, so each has its own variable, endpoint, UUID,
assertion and error message:

| Variable | Panel object | Endpoint | What it is | Where it is attached |
|---|---|---|---|---|
| `profile_name` | Config Profile | `GET /api/config-profiles` | the Xray JSON **the node runs** | Node → `configProfile.activeConfigProfileUuid`; Host → `inbound.configProfileUuid` |
| `xray_json_template_name` | Subscription Template, type `XRAY_JSON` | `GET /api/subscription-templates` | the Xray JSON **a client receives** | Host → `xrayJsonTemplateUuid` |
| `internal_squad_name` | Internal Squad | `GET /api/internal-squads` | which inbounds a user may use | user membership |

A Node has no template field at all: the template is carried by the **Host**. Leaving
`xray_json_template_name` empty means the role does not manage that link and leaves
whatever the panel holds; setting it makes the link declarative, and `node_verify` proves
the published Host really carries it.

Names are matched exactly, as the **API** returns them, not as the UI abbreviates them —
a squad shown as `Default` can be `Default-Squad` in the API. Read them back rather than
copying from the screen:

```bash
curl -sH "Authorization: Bearer $TOKEN" $PANEL/api/config-profiles        | jq -r '.response.configProfiles[].name'
curl -sH "Authorization: Bearer $TOKEN" $PANEL/api/subscription-templates | jq -r '.response.templates[] | select(.templateType=="XRAY_JSON") | .name'
curl -sH "Authorization: Bearer $TOKEN" $PANEL/api/internal-squads        | jq -r '.response.internalSquads[].name'
```

A wrong name fails in controller-side preflight, before the registrar or the server is
touched, and the message names the object it could not find and lists the ones that exist.

## How a Config Profile is written

A Config Profile is the Xray JSON a node runs, and it is named after that node: `TR-01`
runs Config Profile `TR-01`. `profile_name` is therefore derived, not declared — nothing
in this repository carries a profile name as a value, and no default invents one. A
deployment where the whole fleet shares one profile sets `profile_name` (and
`config_profile_create: false`) in `/etc/remnawave/fleet.yml`.

`config_profile_mode` describes how the profile is **written**, not how many nodes use it.
`shared`, the default, is the read-modify-write path below; `per_node` renders the whole
profile from the role's template and replaces anything else it held, which is why it is not
the default even for a profile only one node uses.

Each run reads the profile, merges this node's inbound into it by tag and writes the
result back. Because that is a read-modify-write, the profile is read **again**
immediately before the write and compared by fingerprint with what the run
started from: a second provisioning run, or somebody editing the profile in the
panel, is reported and nothing is written, rather than being silently
overwritten. After the write the run re-reads the profile and fails if any
inbound that was there before has disappeared — the failure that matters is a
write that succeeds and drops another node's traffic. `ansible/tests/test_shared_profile_concurrency.sh`
exercises all of that against a mock panel that changes the profile between the
read and the write. Other nodes' inbounds keep their panel UUIDs and their own Reality keys, and
`routing`, `dns`, `outbounds` and `policy` are copied through untouched. The node's own
Reality keypair is looked up by its own inbound tag, so a node cannot adopt a neighbour's
key. Before the config is pushed to a node the panel removes inbounds of managed
protocols the node does not activate, so a neighbour's Reality key never reaches
it — an inbound of any *other* protocol would go to every node in full, which is
why the profile refuses to carry one unless its tag is listed in
`shared_profile_allowed_unmanaged_inbound_tags`. `inbound_prune_tags` can only
name inbounds inside this node's own tag namespace. The Node object activates only this node's inbound, and every Host is published
against the profile UUID and this node's inbound UUID — the run re-reads the Hosts
afterwards and fails if that binding is not what was declared.

A profile whose name was declared by hand is never invented silently: if it does not exist
the run stops and says so. `config_profile_create=true` overrides that for a first-ever bootstrap of the profile
(and a panel that answers with an empty profile list is treated as a fault, not as
an empty panel),
and `config_profile_require_routing` (on by default) refuses to publish Hosts against a
profile that has no routing rules. `config_profile_mode: per_node` restores the old
behaviour of rendering a private profile per node.

Inventory is the source of truth for names and Panel resources. Change a managed name in
inventory and rerun; editing only in the Panel creates drift that a later run can revert
or reject.

## Certificates

`certificate_mode: http01` is the production mode. Certbot answers the challenge
from `certificate_webroot_path`, which nginx already serves at
`/.well-known/acme-challenge/`, so neither the first issuance nor a renewal takes
the masking site offline. Standalone is used only when nginx is not running yet,
which is the very first installation — there is nothing to interrupt in that case
either. The renewal timer stays idle until the certificate enters the renewal
window, never stops nginx, and reloads it only when the certificate changed.

`certificate_acme_environment` defaults to `auto`, which needs no second command:

* a node with no certificate is issued a **staging** certificate first and a
  **production** one immediately after. The staging attempt costs nothing, and it
  proves DNS, port 80 and the challenge path actually work — so a misconfigured
  node cannot spend Let's Encrypt's production failure budget (5 per hostname per
  hour) discovering that its A record is wrong;
* a node already holding a valid production certificate issues nothing;
* a staging certificate found on the node is replaced with a production one.

Each phase verifies what it installed — the certificate exists and its issuer
matches the environment that phase requested — before the next phase runs, and
the role fails rather than leaving an unexpected certificate in place. Full HTTPS
verification against the public address happens at the end, in `node_verify`:
during the first installation nginx is not running yet, so there is nothing to
probe between the two issuances.

`staging` and `production` still force a single environment for development or
recovery, and `certificate_force_reissue=true` re-issues a certificate that is
still valid.

## What Ansible obtains or creates automatically

The DNS A record. The node's `SECRET_KEY` (once, then reused from the managed `.env`), the
Reality keypair and short ID, this node's inbound and its panel UUID inside the shared
profile, the Node object with its active inbound, every declared Host, the Internal Squad
membership and, when enabled, the bridge service user and the Node Plugin profile. The
certificate. The entire masking site. Deliberate rotation of the node identity requires
the explicit one-run variable `remnawave_rotate_node_secret_key=true`; Reality rotation
requires `reality_rotate_keys=true`. Neither happens as a side effect of a normal run.

Profile or Host changes set `subscription_refresh_required=true` in `set_stats`. The
deployment does not claim clients have cut over before they refresh their subscription.

## Controller prerequisites

`./setup-controller` does all of this; the manual equivalent is here so nothing
is hidden. A Linux controller with Python 3.11–3.13 and OpenSSH, able to reach
the registrar API, the Panel, the node's SSH port and the public selfsteal
address. `sshpass` is additionally needed to log in with a root password; it is
not installed on the managed node. Every path in this project is relative to the
repository root, so the same checkout runs from a laptop, from a controller VPS
and from Semaphore without an environment variable.

```bash
python3 -m venv ansible/.venv
. ansible/.venv/bin/activate
pip install -r ansible/requirements.txt
ansible-galaxy collection install -r ansible/collections/requirements.yml \
  -p ansible/collections
```

The `-p` matters: `ansible.cfg` points `collections_path` into the checkout, so
the collections are pinned with the project and are visible to a service user
that runs with `ProtectHome=true` and cannot read anybody's `~/.ansible`.

## Inventory layout and secrets

Fleet configuration lives next to the playbook, not next to an inventory. That is
not a matter of taste: Semaphore keeps its own inventory outside the repository,
and playbook-adjacent `group_vars` are the only ones that load whatever the
inventory is — and they outrank inventory `group_vars`, so a real value placed
next to an inventory would be silently overridden by the published one.

```text
ansible/
  inventories/<env>/hosts.yml    # one line per node: nothing but addresses
  playbooks/group_vars/
    all/
      panel.yml                  # documentation values: panel URL, profile, zone
      countries.yml              # country code -> label published to users
    remnawave_nodes/
      identity.yml               # everything derived from the hostname
      fleet.yml                  # pinned versions and fleet-wide policy
  examples/
    fleet.yml.example            # -> /etc/remnawave/fleet.yml on the controller
    secrets.yml.example          # -> /etc/remnawave/secrets.yml, ansible-vault
```

Everything tracked here carries documentation values, and preflight **refuses** to
run against them. The real values live on the controller, in two files outside the
checkout:

```text
/etc/remnawave/fleet.yml     # panel URL, CIDRs, zone, squad, ACME email, dns_provider
/etc/remnawave/secrets.yml   # ansible-vault encrypted: panel token, registrar, probe UUID
/etc/remnawave/vault-pass    # the password for the file above
```

Both are loaded as **extra-vars files**: `./provision-node` adds them when they
exist, and every Semaphore template names them in its arguments. That is not a
detail — it is the only mechanism that works for both. Semaphore clones the
repository fresh for every job, so a git-ignored file inside the checkout never
reaches a run started from the UI; and this repository is public, so the real
values cannot be committed. Two files on the controller, read the same way by the
UI and the command line, is what keeps the two paths from drifting.

An encrypted extra-vars file is also how a secret stays off the command line:
Ansible decrypts it in-process, while a secret passed as `-e name=value` — which
is what a Semaphore variable group does — is visible in `/proc` for the duration
of the run.

Two things therefore do **not** belong in a Semaphore variable group. Secrets, for
the reason above. And anything node-specific: variable-group values arrive as
extra vars, which outrank every file *and every host*, so one `node_id` there
gives the whole fleet one identity and each run overwrites the previous node's
panel objects. Preflight recomputes the identity from the inventory hostname and
refuses a run where they disagree, so that mistake now fails instead of being
published.

Examples live in `ansible/examples/` and not in a `group_vars` directory: Ansible
loads every `.yml` it finds there, so an example one rename away from `.yml` would
define real variable names. Regression tests enforce all of this.

`./setup-controller` writes both files and encrypts the second; the manual path is
to copy `examples/fleet.yml.example` and `examples/secrets.yml.example` to
`/etc/remnawave/` and run `ansible-vault encrypt` on the secrets. The Panel token,
registrar credentials, the probe user's VLESS UUID and the bridge password belong
there and nowhere else:

```yaml
vault_remnawave_panel_token: ...
vault_regru_username: ...
vault_regru_password: ...
vault_verify_probe_vless_uuid: ...
vault_node_root_password: ""   # optional: ./provision-node prompts instead
```

The vault password goes in `/etc/remnawave/vault-pass` (`setup-controller` offers
to write it). The wrapper picks it up, and the Semaphore variable group carries
its **path** in `ANSIBLE_VAULT_PASSWORD_FILE` — a path, never the password.

The SSH key is the primary way in and root with a password is the fallback that
survives a wiped `authorized_keys`: `fleet.yml` sets
`node_ssh_allow_root_password: true` for this fleet, `node_base` writes the
highest-priority sshd drop-in and then proves with `sshd -T` that the effective
policy really is what was asked for. The role default stays the safe one
(`false`) for anybody else using these roles. Password login is in any case
reachable only from `management_cidrs`.

`bootstrap.yml` finds the controller's public key itself
(`~/.ssh/remnawave-deployer.pub`, then `~/.ssh/id_ed25519.pub`, then
`~/.ssh/id_rsa.pub`; override `bootstrap_authorized_key_files` to be explicit). A
brand new VPS has no entry in `known_hosts`; `--first-run` (or
`-e bootstrap_trust_new_host_keys=true`) accepts the key presented on first
contact. That is trust-on-first-use — right for a server created minutes ago,
wrong for one that has been running, which is why it is never the default.

The one-off root password reaches the run through one of three places, in the
order that wins: `-e bootstrap_ssh_password=...` (what Semaphore passes from a
secret survey field of that name), `NODE_ROOT_PASSWORD` in the environment (what
`./provision-node` prompts for, never written to disk or into argv), and
`vault_node_root_password` for unattended runs. When any of them is set, the
bootstrap play suppresses its own output and refuses to run at `-vv` or higher,
because Semaphore passes extra vars on the command line.

## Required data beyond the two facts

Per fleet, set once in `/etc/remnawave/fleet.yml` on the controller (written by
`./setup-controller`): Panel URL, `remnawave_panel_cidrs`, `node_domain_zone`,
`acme_email`, the Internal Squad name or UUID, `dns_provider` and the probe user's
username. The controller's own address is discovered, not configured, and no real
address belongs in the tracked files.

`host_specs` is a data model, not a one-Host-per-Node shortcut: a declaration can be
direct, hidden or virtual and can carry an explicit `node_uuids` list. Remnawave 3.3.2
Host tags accept only uppercase letters, digits, `_` and `:`; lowercase tags are rejected
before any Panel mutation.

`verify_tunnel_probe_command` must be a controller-side argv list that returns zero only
after establishing a valid VLESS connection and making an internet request through the new
node. Strict verification is on by default. `verify_node_port_untrusted_probe_host` names
an inventory host whose source IP is outside `remnawave_panel_cidrs`; it proves
`NODE_PORT` really is unreachable for an unrelated source. A bridge must similarly provide
`bridge_spec.entry_inventory_host` and/or `verify_bridge_probe_command`.

## Scoped runs

All of these run from the repository root:

```bash
./provision-node ee01
ansible-playbook ansible/playbooks/install_node.yml --syntax-check
ansible-playbook ansible/playbooks/install_node.yml --limit ee01 --tags preflight --check
ansible-playbook ansible/playbooks/install_node.yml --limit ee01 --tags dns
ansible-playbook ansible/playbooks/install_node.yml --limit ee01 --tags certificate
ansible-playbook ansible/playbooks/install_node.yml --limit ee01 --tags selfsteal
ansible-playbook ansible/playbooks/install_node.yml --limit ee01 --tags node_verify
```

## Running from Semaphore

Nothing here points at a specific machine, needs an interactive prompt or reads a
secret from outside the vault, so the move is configuration only. Semaphore
clones the repository and runs playbooks from its root, which is exactly how the
wrappers and the tests run them.

Three templates, and deliberately no others:

| Template | Playbook and flags | Changes anything? |
|---|---|---|
| **Preflight** | `ansible/playbooks/provision_node.yml`, `--tags preflight --check` | no |
| **Install / Reconcile Node** | `ansible/playbooks/provision_node.yml` | yes |
| **Verify Node** | `ansible/playbooks/provision_node.yml`, `--tags node_verify` | no |

Reconcile *is* update and *is* repair, so there is no separate button for either:
one mutating template means one thing to get right. Dangerous operations —
`certificate_force_reissue`, `reality_rotate_keys`,
`remnawave_rotate_node_secret_key`, `inbound_prune_tags` — stay command-line
only, with the variable spelled out by hand.

- **Target** goes in the run's *Limit* field. In Semaphore 2.18.29 that is a free
  text field, not a list built from the inventory; there is no dropdown to be had,
  so nothing here pretends otherwise and nothing duplicates the node list into a
  survey. The playbook refuses a run that resolves to more than one host unless
  `node_allow_bulk=true`, so a forgotten Limit is an error rather than an
  incident.
- **Inventory** is a static YAML inventory in Semaphore, holding the same one line
  per node as `hosts.yml`. It is the node registry, so it is the one piece of
  state that does not live in git — back it up.
- **Deployment values** are `-e '@/etc/remnawave/fleet.yml' -e '@/etc/remnawave/secrets.yml'`
  in each template's arguments, and `ANSIBLE_VAULT_PASSWORD_FILE` in the variable
  group's environment (a path, not a password). A job runs from a fresh clone, so
  this is the only way the real panel URL, zone and secrets reach it.
  `semaphore_bootstrap.py` sets all three.
- **The variable group holds no values at all** beyond that one environment
  variable. Its values arrive as extra vars, which outrank every file and every
  host: a secret there is visible in `/proc` for the length of the run, and a
  `node_id` there gives the whole fleet one identity.
- **The root password of a brand new VPS** is a *secret survey field* named
  `bootstrap_ssh_password`. Survey secrets are not stored in Semaphore's database
  (`Task.Secret` is `db:"-"`), which is exactly what a one-off credential wants;
  a Variable Group keeps its secrets at rest and is for the long-lived ones — the
  panel token, the vault password. Either way the value reaches Ansible as
  `--extra-vars` on the command line, so the controller must stay
  single-purpose and the template must not raise verbosity: the bootstrap play
  refuses to run with a password at `-vv` or higher.
- **`--first-run`** has no wrapper in the UI: pass
  `bootstrap_trust_new_host_keys=true` as a survey checkbox used only for a
  server created minutes ago.
- **Parallelism** is disabled on the Install / Reconcile template only
  (`allow_parallel_tasks: false`). Preflight and Verify are read-only and may run
  while an installation is in progress.

The controller address changes when the runner does, so the registrar's API
allow-list must contain the Semaphore host's address; `management_cidrs` does not
need updating, because it is discovered from the live SSH connection. Add a
workstation or rescue host to `management_cidrs_extra` in the git-ignored
override so it keeps access when nobody is running from it.

## The controller

One role builds it, and it targets a `controller` group so the same code works
from a workstation and on the machine itself:

```bash
ansible-playbook ansible/playbooks/controller.yml --limit controller --check
ansible-playbook ansible/playbooks/controller.yml --limit controller
```

It installs the pinned Ansible runtime and the collections into the checkout, a
pinned Semaphore (2.18.29, verified against a checksum, and the running binary is
checked against the pin afterwards), the systemd unit, swap, unattended upgrades,
fail2ban and a nightly backup. Three things it deliberately does not do:

- **No Docker.** The controller runs playbooks; it does not run workloads. A
  regression test forbids Docker module calls anywhere in the role.
- **No change to SSH authentication.** This is the machine an operator has to be
  able to get back into, so root and password login are left exactly as the
  provider set them. A regression test forbids touching `sshd_config`.
- **No public listener.** The UI binds to loopback only; the role refuses any
  other `semaphore_interface` and then checks with `ss` that nothing is listening
  on all interfaces. Reach it with
  `ssh -N -L 3000:127.0.0.1:3000 <controller>`.

Two properties matter for adopting a controller that was installed by hand.
`semaphore_config_path` is read first, and the three encryption keys found there
are **reused**, never regenerated — new keys would make every stored Key Store
entry undecryptable. And the database dialect of an existing instance is never
changed: if the declaration disagrees with the instance, the run stops and says
so rather than inventing a migration.

The firewall follows the node's pattern: the address of the live session is
always in the allow list, `nft -c` validates before anything is applied, a
rollback timer is armed, and the connection is re-established and proven before
the timer is disarmed.

The Semaphore project, repository, inventory and the three templates are created
by `ansible/tools/semaphore_bootstrap.py`, so a rebuilt controller gets the same
templates with the same flags instead of whatever somebody remembers:

```bash
export SEMAPHORE_URL=http://127.0.0.1:3000
export SEMAPHORE_API_TOKEN="$(cat /etc/semaphore/bootstrap-api-token)"
python3 ansible/tools/semaphore_bootstrap.py --dry-run   # prints, changes nothing
python3 ansible/tools/semaphore_bootstrap.py
```

It is idempotent by name and never writes a secret: the deployer key, the vault
password and the panel token are added in the UI, and it prints exactly what is
left to do by hand.

## Publishing the UI

By default the UI is reachable only through an SSH tunnel. It can be published on
a name instead, and the shape of that is deliberate: **Semaphore never leaves
loopback**. What listens on the public address is nginx, whose whole job is TLS,
deciding who may knock, and passing the request to `127.0.0.1:3000`. A mistake in
the proxy configuration cannot expose the admin panel directly, and the role
checks with `ss` afterwards that 443 is public and the UI port is not.

```yaml
# /etc/remnawave/controller.yml
controller_proxy_enabled: true
controller_proxy_domain: web.your-domain.tld
controller_proxy_acme_email: ops@your-domain.tld
controller_proxy_allowed_cidrs: [203.0.113.9/32, 198.51.100.0/24]
controller_proxy_basic_auth_users:
  - {user: ops, hash: "$apr1$..."}
```

```bash
ansible-playbook ansible/playbooks/controller.yml \
  -i 'localhost,' -c local -e controller_group=localhost \
  -e @/etc/remnawave/controller.yml
```

What that gets you, and why each part is there:

- **TLS from Let's Encrypt over HTTP-01.** The first pass installs an HTTP-only
  site that answers `/.well-known/acme-challenge/`, issues the certificate, and
  only then writes the TLS site — so nothing is ever published without a
  certificate. Renewal is certbot's own timer plus a deploy hook that reloads
  nginx, because nginx keeps serving the old certificate until told otherwise.
  Preflight refuses to run certbot at all if the name does not resolve to this
  host: five failures per hour per hostname is the whole budget.
- **An address list, and it is the control that matters.** Behind this login sits
  the machine that provisions every node, so limiting who can reach the login page
  is worth more than any header. `controller_proxy_allowed_cidrs` becomes both an
  nginx `allow`/`deny` and an nftables rule. Leaving it empty publishes the panel
  to the internet and requires `controller_proxy_ack_public: true` — an explicit
  decision, not a default.
- **Basic auth in front of Semaphore's own login.** Optional, cheap, and it is
  what stops credential stuffing and scanners from ever reaching the login form.
  Hashes only (`openssl passwd -apr1`); preflight rejects a value that looks like
  a plaintext password, because nginx would accept it and authenticate nobody.
- **The unauthenticated routes are blocked.** Verified against 2.18.29:
  `/api/integrations/{alias}`, `/api/terraform/{alias}` and `/api/internal/runners`
  are served with **no authentication**. Harmless on loopback; published, an alias
  somebody guesses runs a task against the fleet. The proxy returns 404 for all
  three. `/api/auth/recovery` stays reachable — it is the login page's own flow.
- **Live task output keeps working.** Semaphore streams it over a WebSocket, so the
  proxy passes `Upgrade`/`Connection` through (with the `map` that needs to live in
  the `http` context) and raises the read timeout to an hour. Without that the UI
  loads and then silently never shows a running task.
- **The firewall opens 80 and 443 only.** Never the UI port — Semaphore binds
  loopback, so there is nothing there to open. Port 80 stays open to everyone even
  when the allow list is set, because Let's Encrypt validates from addresses nobody
  can predict; nginx serves nothing there but the challenge path and a redirect.
- **`web_host` follows the public name.** Semaphore builds links and cookie scope
  from it; left pointing at loopback, a published instance redirects people to
  `127.0.0.1`.

### Accounts

Semaphore 2.18.29 has **no self-registration route at all** — there is no setting
to switch off, because accounts exist only because an admin created them. Two
things are still worth setting, and the role sets both:
`non_admin_can_create_project: false` and `password_login_disable: false` (people
do need to log in).

A second operator is created without touching the UI:

```bash
export SEMAPHORE_API_TOKEN='...' SEMAPHORE_NEW_USER_PASSWORD='...'
python3 ansible/tools/semaphore_bootstrap.py \
  --add-user mate --user-name "Second operator" --user-email mate@example.com
```

Not an admin, and `task_runner` in the project by default: they may press the
three buttons and read the logs, but cannot rewrite a template into something
else. `--user-role manager` if they should also edit templates. The password comes
from the environment for that one command and is never written anywhere in this
repository; the account is created only if the login does not already exist, so
re-running never resets somebody's password.

## End-to-end probe

Strict acceptance means a node is not "ready" until traffic has gone through it.
The built-in probe connects from the controller exactly as a client would — VLESS
over Reality with Vision to the published Host — and fetches one URL through it.
Everything it needs is already known from the run except the client identity, so
the one-time setup is two things:

1. an Xray client binary on the controller (`verify_probe_xray_binary`, default
   `xray` in `PATH`) — the controller has no Docker, so this is a plain binary;
2. an existing panel user to connect as: its VLESS UUID in
   `vault_verify_probe_vless_uuid`, and its username in `verify_probe_username`.

The probe never creates or changes that user: before connecting it reads the
user through the API and requires it to be `ACTIVE` and a member of the Internal
Squad this fleet publishes to, which is what gives it access to the node's
inbound. Verification that mutates the panel is not verification.

The Reality public key is derived from the node's own private key on the
controller (`remnawave_reality_public_key`, x25519 via `cryptography`), so
nothing has to be stored twice and no Docker is needed to compute it.

`verify_require_tunnel_probe` stays `true`. With neither a probe identity nor a
custom `verify_tunnel_probe_command`, the run **fails** — reporting a node nobody
proved carries traffic is worse than a red run.
`ansible/tests/test_tunnel_probe.sh` covers the fail-closed path, the three
panel-state refusals and a full pass through a real SOCKS proxy to a real HTTP
endpoint; only the Reality handshake itself needs a node.

## Backup and restore

The Semaphore inventory is the node registry, and it is the one piece of state
that does not live in git. The nightly job therefore covers it and says so
loudly when it cannot:

- the database (for `bolt`, the service is stopped for the copy — bolt has no
  online snapshot and copying a file being written to can capture a torn page;
  the window is under a second on a single-user UI);
- a portable export of every project **including its inventory**, taken through
  the API so it can be read and restored without this exact binary;
- `config.json` with its secrets redacted;
- a manifest with the sha256 of every member and the pinned versions.

The Semaphore encryption keys, the Ansible vault password and the deployer
private key are deliberately **not** in the archive: one file holding both the
ciphertext and its key loses everything at once. They belong in a password
manager, and the restore procedure expects them from there.

`controller_backup_remote` is an rsync destination. Left empty, the job still
runs, still writes the archive and then **exits non-zero**, so the timer's status
shows a failure — an archive that never leaves the machine it protects is not a
backup.

Restore on a clean Debian 13: install the controller with
`ansible-playbook ansible/playbooks/controller.yml`, put the three encryption
keys from the password manager into `config.json` before the first start, restore
the database file from the archive (or import the project export through the UI),
restore the deployer key and the vault password into the Key Store, then run the
**Preflight** template against one node. A restore that has not been verified by
a green Preflight has not been verified.

## Verification and tests

Everything below runs from the repository root and needs no server:

```bash
yamllint -c .yamllint.yml ansible .github
python ansible/tests/validate_structure.py
python -m unittest discover -s ansible/tests -p 'test_*.py'
ansible-playbook ansible/playbooks/provision_node.yml --syntax-check
bash ansible/tests/test_external_inventory.sh
ansible-playbook -i localhost, -c local ansible/tests/management_cidrs.yml
ansible-playbook -i localhost, -c local ansible/tests/render_templates.yml
bash ansible/tests/test_panel_idempotency.sh
bash ansible/tests/test_shared_profile_concurrency.sh
bash ansible/tests/test_panel_bridge_idempotency.sh
bash ansible/tests/test_panel_errors.sh
bash ansible/tests/test_dns_idempotency.sh
bash ansible/tests/validate_nginx.sh
ansible-lint
```

CI runs the first group on every push and the container-and-mock group on a pull
request, so a mistake is visible in a minute and the slow proof still gates a
merge.

Full acceptance on a test VPS — including recovery from a failed run, two strict runs,
VPN end-to-end, bridge and the no-secret-rotation checks — is described in
[TESTING.ru.md](TESTING.ru.md).

Static checks need no VPS:

```bash
yamllint -c .yamllint.yml .
ansible-playbook -i inventories/staging/hosts.yml playbooks/provision_node.yml --syntax-check
ansible-playbook -i inventories/staging/hosts.yml tests/inventory_derivation.yml
ansible-lint
python -m unittest discover -s tests -p 'test_*.py'
ansible-playbook -i localhost, -c local tests/render_templates.yml
python tests/validate_structure.py
bash tests/test_panel_idempotency.sh
bash tests/test_panel_bridge_idempotency.sh
bash tests/test_panel_errors.sh
bash tests/test_dns_idempotency.sh
bash tests/validate_nginx.sh
(cd roles/node_base && molecule test)
(cd roles/remnawave_node && molecule test)
```

`test_panel_idempotency.sh` starts from a seeded Config Profile that already holds another
node's inbound and the routing rules, then proves the second run changes nothing, the
foreign inbound keeps its UUID and Reality key, the routing survives, and the Host and Node
are bound to that profile. `test_dns_idempotency.sh` runs the DNS role against a
stateful mock registrar and covers create, retarget, no-op, an ambiguous name and an
unknown zone, asserting that foreign records survive. The decoy tests assert that one seed
gives a byte-identical site, that 64 different nodes produce 64 different pages, class name
sets, asset names and response sizes, and that a re-render reports no change.
`validate_nginx.sh` renders the configuration against a throwaway certificate and checks it
with `nginx -t` in the pinned image, falling back to a local nginx binary and then to a
crossplane syntax check — it prints which path it took, because only the first two check
directive semantics.

Full acceptance still requires a real VPS, DNS, ACME, a test Panel, a valid test
subscription and, for bridge mode, an entry node. Those external checks are enforced by
`node_verify` rather than replaced with a passing stub. **Nothing in this project has been
run against a real server yet, so it is not production-ready.**

## Operational behavior

The managed nftables table is isolated and validated with `nft -c`; a timer restores the
previous ruleset if the controller cannot reconnect. RemnaNode control traffic is accepted
only from `remnawave_panel_cidrs`, bridge traffic only from the entry address, and Xray's
internal API is never exposed. Xray writes its persisted log to
`/var/log/remnanode/current`, nginx is recreated after a validated bind-mounted
configuration change, verified geodata replaces files atomically, and handlers restart only
the affected service.
