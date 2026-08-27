# Ansible deployment for Remnawave nodes

This directory is the non-interactive replacement for `remnawave-node.sh`. It does not
create a VPS and does not diagnose TSPU blocking. Everything after the server exists —
DNS, the operating system, the Remnawave Panel resources, the node runtime, the masking
site and the acceptance checks — is owned here.

## One command per node

```yaml
# inventories/production/hosts.yml
ee01:
  ansible_host: 203.0.113.10
  node_id: ee_01
```

```bash
ansible-playbook playbooks/provision_node.yml --limit ee01 --ask-vault-pass
```

`provision_node.yml` runs bootstrap and installation in order and is safe to re-run: it
decides for itself whether the managed account still has to be created. `bootstrap.yml`
and `install_node.yml` remain separately runnable for debugging and maintenance.

Everything else is derived from those two facts in
`inventories/*/group_vars/remnawave_nodes.yml`:

| Derived value | From |
|---|---|
| `node_name` (`EE-01`) | `node_id` uppercased, `_` to `-` |
| `node_country` (`EE`) | first segment of `node_id` |
| `node_public_ip` | `ansible_host` |
| `selfsteal_domain` (`ee01.<zone>`) | `node_id` without `_`, plus `node_domain_zone` |
| `inbound_specs[0].tag` (`EE_01_REALITY`) | `node_name` |
| `host_specs[0]` | `node_name`, `selfsteal_domain`, `node_country` |
| `selfsteal_virtual_hosts` | `selfsteal_domain` |
| the entire decoy site | `node_id` (see below) |

Any derivation can be overridden per host — a node whose domain does not follow the zone
pattern simply declares `selfsteal_domain` in its own block.

## Roles and execution order

`install_node.yml` contains no deployment tasks and runs five roles in order.

`dns` reconciles the node's A record at the registrar from the controller, then waits
until public resolvers agree. It runs first because the node's own preflight resolves the
selfsteal name and ACME needs the record to be live. `node_base` validates the host,
installs base packages and Docker, then applies a rollback-protected nftables policy.
`remnawave_panel` reconciles the shared Config Profile, this node's inbound, the Node, its
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
verifies the effective policy with `sshd -T`.

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

## One shared Config Profile

All nodes share a single Config Profile — `Default August` by default — because that
profile is the Xray JSON that carries the routing every published Host depends on. A node
never owns a private copy of it.

Each run reads the profile, merges this node's inbound into it by tag and writes the
result back. Other nodes' inbounds keep their panel UUIDs and their own Reality keys, and
`routing`, `dns`, `outbounds` and `policy` are copied through untouched. The node's own
Reality keypair is looked up by its own inbound tag, so a node cannot adopt a neighbour's
key. The Node object activates only this node's inbound, and every Host is published
against the shared profile UUID and this node's inbound UUID — the run re-reads the Hosts
afterwards and fails if that binding is not what was declared.

The shared profile is never invented silently: if it does not exist the run stops and says
so. `config_profile_create=true` overrides that for a first-ever bootstrap of the profile,
and `config_profile_require_routing` (on by default) refuses to publish Hosts against a
profile that has no routing rules. `config_profile_mode: per_node` restores the old
behaviour of rendering a private profile per node.

Inventory is the source of truth for names and Panel resources. Change a managed name in
inventory and rerun; editing only in the Panel creates drift that a later run can revert
or reject.

## Certificates

`certificate_mode: http01` is the production mode. Certbot answers the challenge from
`certificate_webroot_path`, which nginx already serves at `/.well-known/acme-challenge/`,
so neither the first issuance nor a renewal takes the masking site offline. Standalone is
used only when nginx is not running yet, which is the very first installation — there is
nothing to interrupt in that case either. The renewal timer stays idle until the
certificate enters the renewal window, never stops nginx, and reloads it only when the
certificate actually changed.

`certificate_acme_environment` selects the CA: `staging` for iteration (untrusted
certificates, no production rate limits) and `production` for real nodes. Switching
environments is detected from the certificate issuer and forces a clean re-issue, and the
run fails if the installed certificate does not match the requested environment — a
staging certificate can never quietly reach production.

`cloudflare_dns` mode still exists but is unused while the zones live at REG.RU.

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

A Linux controller with Python 3.11–3.13 and OpenSSH, able to reach the registrar API, the
Panel, the node's SSH port and the public selfsteal address. Password-based SSH
additionally needs `sshpass` on the controller; it is not installed on the managed node.
All paths in this project are relative to this directory, so the same code runs from a
laptop and, later, from Semaphore.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
ansible-galaxy collection install -r collections/requirements.yml
```

## Inventory layout and secrets

```text
inventories/<env>/
  hosts.yml                      # two facts per node
  group_vars/
    all/
      panel.yml                  # panel URL/token, shared profile, CIDRs, ACME email, zone
      vault.yml                  # ansible-vault encrypted secrets
    remnawave_nodes.yml          # derivations and fleet-wide runtime values
```

`inventories/staging` is the documented reference and carries placeholder values only.
Files that hold real addresses are git-ignored — `inventories/test/`,
`inventories/local/`, `inventories/production/hosts.yml` and
`inventories/production/group_vars/all/panel.yml` — with `*.yml.example` twins in git. An
example file must never end in `.yml`: Ansible loads every `.yml` file in `group_vars`, so
`vault.example.yml` would define real variables. A regression test enforces that.

Copy `group_vars/all/vault.yml.example` to `vault.yml`, fill it in and encrypt it with
`ansible-vault encrypt`. The Panel token, registrar credentials, root password,
`SECRET_KEY`, Reality private key, bridge password and Cloudflare token belong there and
nowhere else:

```yaml
vault_remnawave_panel_token: ...
vault_regru_username: ...
vault_regru_password: ...
vault_node_root_password: ...
```

A node that must keep root/password access sets `ansible_user: root`,
`ansible_password: "{{ vault_node_root_password }}"`, `ansible_become: false` and
`node_ssh_allow_root_password: true` in its host block. Root/password access is off by
default and, when enabled, is still limited to `management_cidrs`.

`bootstrap.yml` needs the controller's public key in `bootstrap_authorized_keys` and, for
a server that only accepts a root password, `bootstrap_ssh_password` (or `--ask-pass`).
A brand new VPS has no entry in `known_hosts`; set `bootstrap_trust_new_host_keys=true`
to accept the key presented on first contact, or add the fingerprint from the provider
console beforehand. That flag is trust-on-first-use — right for a server created minutes
ago, wrong for one that has been running.

## Required data beyond the two facts

Per fleet, set once in `group_vars/all/panel.yml`: Panel URL and token, `management_cidrs`
(the address sshd actually sees — preflight refuses to apply a firewall that would lock
the controller out), `remnawave_panel_cidrs`, `node_domain_zone`, `acme_email` and the
Internal Squad name or UUID.

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

```bash
ansible-playbook playbooks/provision_node.yml --limit ee01
ansible-playbook playbooks/install_node.yml --syntax-check
ansible-playbook playbooks/install_node.yml --tags preflight --check
ansible-playbook playbooks/install_node.yml --tags dns
ansible-playbook playbooks/install_node.yml --tags certificate
ansible-playbook playbooks/install_node.yml --tags selfsteal
ansible-playbook playbooks/install_node.yml --tags node_verify
```

## Running from Semaphore later

Nothing here points at a specific machine, needs an interactive prompt or reads a secret
from outside the vault, so the move is configuration only: point the Semaphore repository
at this repo, set the playbook path to `ansible/playbooks/provision_node.yml`, add the
vault password as a Semaphore secret, and either commit an inventory that carries no real
addresses or paste the inventory into Semaphore. The controller address changes when the
runner does, so `management_cidrs` and the registrar's API allow-list must then contain the
Semaphore host's address — preflight will say so plainly if the first does not.

## Verification and tests

Full acceptance on a test VPS — including recovery from a failed run, two strict runs,
VPN end-to-end, bridge and the no-secret-rotation checks — is described in
[TESTING.ru.md](TESTING.ru.md).

Static checks need no VPS:

```bash
yamllint -c .yamllint.yml .
ansible-playbook -i inventories/staging/hosts.yml playbooks/provision_node.yml --syntax-check
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

`test_panel_idempotency.sh` starts from a seeded shared profile that already holds another
node's inbound and the routing rules, then proves the second run changes nothing, the
foreign inbound keeps its UUID and Reality key, the routing survives, and the Host and Node
are bound to the shared profile. `test_dns_idempotency.sh` runs the DNS role against a
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
