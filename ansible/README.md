# Ansible deployment for Remnawave nodes

This directory contains the non-interactive replacement for `remnawave-node.sh`. It creates no VPS and does not diagnose TSPU blocking; Terraform or another infrastructure controller must provide a reachable Debian 12, Debian 13 or Ubuntu 24.04 VPS, DNS, provider firewall and SSH credentials. Ansible owns the operating system, Remnawave Panel resources, node runtime and acceptance checks.

## Roles and execution order

The playbook contains no deployment tasks and runs exactly four roles. `node_base` validates the host, installs the base OS packages and Docker, then applies a rollback-protected nftables policy. `remnawave_panel` reconciles the Config Profile, inbounds, Node, Hosts, Internal Squad and optional bridge identity through the Remnawave 3.x API. `remnawave_node` installs the pinned Xray binary, certificate, nginx selfsteal virtual hosts, RemnaNode, optional geodata/plugins and maintenance units. `node_verify` checks the actual containers, listeners, certificate, firewall, public selfsteal response, Panel links and optional bridge/tunnel probes.

Small components are task files inside those roles. They are not additional roles.

## Controller prerequisites

Use a Linux Ansible Controller with Python 3.11–3.13, OpenSSH and access to the Panel, node SSH port and public selfsteal address. Password-based SSH additionally requires `sshpass` on the Controller; it is not installed on the managed node. Install dependencies from this directory:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
ansible-galaxy collection install -r collections/requirements.yml
```

Copy `inventories/staging/group_vars/all/vault.example.yml` to `vault.yml` in the same directory, replace its values and encrypt it with `ansible-vault encrypt`. A node that must retain root/password access sets `ansible_user: root`, `ansible_password: "{{ vault_node_root_password }}"`, `ansible_become: false` and `node_ssh_allow_root_password: true` in its host variables. Root/password access is disabled by default and, when explicitly enabled, remains limited by `management_cidrs`. Do not put the password, Panel token, `SECRET_KEY`, Reality private key, bridge password or Cloudflare token in Terraform state or unencrypted inventory.

## Required data

The staging inventory documents the complete first-iteration input. At minimum each node needs stable `node_id`/`node_name`, public IP, country, selfsteal domain, Panel URL/token, management and Panel CIDRs, one `inbound_specs` entry, one or more `host_specs`, an Internal Squad name/UUID, pinned images and official Xray checksums. `host_specs` is a data model, not a one-Host-per-Node shortcut: a declaration can be direct, hidden or virtual and can carry an explicit `node_uuids` list. Remnawave 3.3.2 Host tags must use only uppercase letters, digits, `_` and `:`; lowercase tags are rejected before any Panel mutation.

Inventory is the source of truth for names and Panel resources. Change managed names in inventory and rerun Ansible; editing only in Panel creates drift that a later run can revert or reject.

For a bridge exit, set `bridge_spec.enabled: true` and provide a stable ID, entry address, inbound tag, port, method and service user. The role reuses the existing user's `ssPassword`; it never rotates a working bridge secret during a normal run. The bridge inbound is active on the Node but is not published as a normal Host or added to the users' Squad.

`verify_tunnel_probe_command` must be a Controller-side argv list that returns zero only after establishing a valid VLESS connection and making an internet request through the new node. Strict verification is enabled by default. Set `verify_node_port_untrusted_probe_host` to an inventory host whose source IP is outside `remnawave_panel_cidrs`; it verifies that `NODE_PORT` is actually unreachable for an unrelated source. A bridge must similarly provide `bridge_spec.entry_inventory_host` and/or `verify_bridge_probe_command`.

## Deployment

Run from the `ansible` directory so relative paths and configuration are deterministic:

```bash
ansible-playbook -i inventories/production/hosts.yml playbooks/install_node.yml --ask-vault-pass
```

Useful read-only or scoped runs are:

```bash
ansible-playbook playbooks/install_node.yml --syntax-check
ansible-playbook playbooks/install_node.yml --tags preflight --check
ansible-playbook playbooks/install_node.yml --tags node_verify
```

The Panel role uses GET → normalized comparison → POST/PATCH → GET. It fails on ambiguous matches and never deletes foreign resources. The first installation obtains `SECRET_KEY` once and the node role stores it in the managed `.env`; subsequent runs read and reuse that identity instead of calling `/api/keygen`. Deliberate certificate rotation requires the explicit one-run variable `remnawave_rotate_node_secret_key=true`, which must be removed immediately after the successful run. Profile or Host changes set `subscription_refresh_required=true` in `set_stats`; the deployment does not claim that clients have cut over before they refresh their subscription. A second run with unchanged input should report no managed changes.

## Verification and tests

Полная приёмка на тестовой VPS, включая восстановление после ошибки, два строгих запуска, VPN E2E, bridge и проверку отсутствия ротации секретов, описана в [TESTING.ru.md](TESTING.ru.md).

Static checks do not need a VPS:

```bash
yamllint -c .yamllint.yml .
ansible-playbook -i inventories/staging/hosts.yml playbooks/install_node.yml --syntax-check
ansible-lint
python -m unittest discover -s tests -p 'test_*.py'
ansible-playbook -i localhost, -c local tests/render_templates.yml
python tests/validate_structure.py
bash tests/test_panel_idempotency.sh
bash tests/test_panel_bridge_idempotency.sh
bash tests/test_panel_errors.sh
(cd roles/node_base && molecule test)
(cd roles/remnawave_node && molecule test)
```

Molecule scenarios exercise base provisioning and pinned Xray installation on Debian 12, Debian 13 and Ubuntu 24.04 in privileged disposable containers. Full acceptance still requires a real VPS, DNS, ACME, a test Remnawave Panel, a valid test subscription and, for bridge mode, an entry node. These external checks are deliberately enforced by `node_verify` rather than replaced with a successful stub.

## Operational behavior

The managed nftables table is isolated and validated with `nft -c`; a timer restores the previous ruleset if the Controller cannot reconnect. RemnaNode control traffic is accepted only from `remnawave_panel_cidrs`, bridge traffic only from the entry address, and Xray's internal API is never opened. Certificates are inspected before Certbot runs; the renewal timer does not stop nginx until the certificate enters the renewal window and restores it even if Certbot fails. Profiles reuse existing Reality keys, Xray writes its persisted log to `/var/log/remnanode/current`, nginx is recreated after a validated bind-mounted configuration change, verified geodata replaces files atomically, and handlers restart only the affected service.
