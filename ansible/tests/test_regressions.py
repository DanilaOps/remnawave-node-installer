from __future__ import annotations

import pathlib
import unittest

import yaml


ROOT = pathlib.Path(__file__).parents[1]


class ProductionRegressionTests(unittest.TestCase):
    def read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_secret_key_is_reused_unless_rotation_is_explicit(self) -> None:
        defaults = yaml.safe_load(self.read("roles/remnawave_panel/defaults/main.yml"))
        tasks = self.read("roles/remnawave_panel/tasks/authenticate.yml")
        self.assertFalse(defaults["remnawave_rotate_node_secret_key"])
        self.assertIn("remnawave_existing_secret_keys", tasks)
        self.assertIn("remnawave_rotate_node_secret_key", tasks)
        self.assertIn("Persist RemnaNode identity before further Panel mutations", tasks)
        self.assertIn("dest: \"{{ remnawave_node_env_path }}\"", tasks)

    def test_xray_log_mount_and_healthcheck_match_remnanode_332(self) -> None:
        compose = self.read("roles/remnawave_node/templates/compose.yml.j2")
        logrotate = self.read("roles/remnawave_node/templates/remnawave.logrotate.j2")
        self.assertIn("{{ remnawave_logs_dir }}:/var/log/xray", compose)
        self.assertNotIn("{{ remnawave_logs_dir }}:/var/log/remnanode", compose)
        self.assertIn("/command/s6-svstat /run/service/xray", compose)
        self.assertIn("{{ remnawave_logs_dir }}/current", logrotate)

    def test_nginx_update_forces_service_recreation(self) -> None:
        handlers = self.read("roles/remnawave_node/handlers/main.yml")
        self.assertIn("Recreate selfsteal nginx", handlers)
        self.assertIn("recreate: always", handlers)

    def test_certificate_renewal_never_stops_nginx(self) -> None:
        service = self.read("roles/remnawave_node/templates/remnawave-cert-renew.service.j2")
        helper = self.read("roles/remnawave_node/templates/remnawave-cert-renew.sh.j2")
        self.assertNotIn("ExecStartPre", service)
        self.assertNotIn("ExecStartPost", service)
        # Renewal stays idle outside the window and answers HTTP-01 from the
        # webroot nginx already serves, so the decoy site is never taken down.
        self.assertIn("openssl x509 -checkend", helper)
        self.assertIn("--webroot --webroot-path", helper)
        self.assertNotIn("docker stop", helper)

    def test_certificate_issuance_prefers_webroot_over_stopping_nginx(self) -> None:
        plan = self.read("roles/remnawave_node/tasks/certificate.yml")
        issue = self.read("roles/remnawave_node/tasks/certificate_issue.yml")
        self.assertNotIn("state: stopped", plan)
        self.assertNotIn("state: stopped", issue)
        self.assertIn("remnawave_certificate_authenticator", issue)
        # Standalone is only reachable when nginx is not running yet.
        self.assertIn(
            "{{ 'webroot'\n         if (remnawave_nginx_before_acme.container.State.Running",
            issue,
        )

    def test_acme_auto_mode_issues_staging_before_production(self) -> None:
        defaults = yaml.safe_load(self.read("roles/remnawave_node/defaults/main.yml"))
        tasks = self.read("roles/remnawave_node/tasks/certificate.yml")
        issue = self.read("roles/remnawave_node/tasks/certificate_issue.yml")
        self.assertEqual(defaults["certificate_acme_environment"], "auto")
        self.assertIn("acme-staging-v02", defaults["certificate_acme_directories"]["staging"])
        # A node with no certificate proves the challenge path against the staging
        # CA before spending a production issuance; anything else is one phase.
        self.assertIn("['staging', 'production']", tasks)
        self.assertIn("remnawave_certificate_phases", tasks)
        self.assertIn("Require the installed certificate to match the target ACME environment", tasks)
        # Each phase verifies what it installed before the next one runs.
        self.assertIn("--force-renewal", issue)
        self.assertIn("Require the installed certificate to match phase", issue)
        self.assertNotIn("state: stopped", issue)

    def test_hosts_are_bound_to_the_shared_profile(self) -> None:
        defaults = yaml.safe_load(self.read("roles/remnawave_panel/defaults/main.yml"))
        profile = self.read("roles/remnawave_panel/tasks/profile.yml")
        hosts = self.read("roles/remnawave_panel/tasks/hosts.yml")
        host_item = self.read("roles/remnawave_panel/tasks/host_item.yml")
        self.assertEqual(defaults["config_profile_mode"], "shared")
        self.assertEqual(defaults["profile_name"], "Default August")
        self.assertFalse(defaults["config_profile_create"])
        self.assertTrue(defaults["config_profile_require_routing"])
        # The shared profile is merged, never overwritten, and is never invented.
        self.assertIn("remnawave_upsert_inbounds", profile)
        self.assertIn("Require the shared Config Profile to exist", profile)
        self.assertIn("Require the managed profile to carry routing rules", profile)
        self.assertIn("Confirm the reconciled profile is the one named in inventory", profile)
        self.assertIn("configProfileUuid", host_item)
        self.assertIn("Guarantee every managed Host is published", hosts)

    def test_node_and_host_comparisons_are_normalized(self) -> None:
        node = self.read("roles/remnawave_panel/tasks/node.yml")
        host_item = self.read("roles/remnawave_panel/tasks/host_item.yml")
        # The panel returns activeInbounds and nodes as objects; comparing them
        # against uuid strings made every run report a change.
        self.assertIn("remnawave_normalize_node_links", node)
        self.assertIn("remnawave_normalize_host_links", host_item)
        self.assertNotIn('port: "{{ remnawave_node_port | int }}"', node)

    def test_bootstrap_does_not_fight_node_base_over_sshd(self) -> None:
        tasks = self.read("roles/node_bootstrap/tasks/main.yml")
        self.assertIn("bootstrap_authorized_keys", tasks)
        self.assertIn("visudo -cf %s", tasks)
        # sshd policy has exactly one owner: node_base.
        self.assertNotIn("sshd_config", tasks)
        self.assertNotIn("PasswordAuthentication", tasks)

    def test_bridge_patch_uses_supported_user_identity(self) -> None:
        bridge = self.read("roles/remnawave_panel/tasks/bridge_user.yml")
        self.assertIn('username: "{{ bridge_spec.user }}"', bridge)
        self.assertNotIn("remnawave_bridge_user_lookup.json.response.uuid", bridge)

    def test_debian_13_has_role_and_molecule_coverage(self) -> None:
        defaults = yaml.safe_load(self.read("roles/node_base/defaults/main.yml"))
        self.assertIn("13", defaults["node_base_supported_os"]["Debian"])
        for role in ("node_base", "remnawave_node"):
            molecule = self.read(f"roles/{role}/molecule/default/molecule.yml")
            self.assertIn("docker-debian13-ansible", molecule)

    def test_root_password_ssh_is_explicit_and_cloud_init_safe(self) -> None:
        defaults = yaml.safe_load(self.read("roles/node_base/defaults/main.yml"))
        template = self.read("roles/node_base/templates/90-remnawave-ssh.conf.j2")
        system_tasks = self.read("roles/node_base/tasks/system.yml")
        self.assertFalse(defaults["node_ssh_allow_root_password"])
        self.assertIn("node_ssh_allow_root_password", template)
        self.assertIn("dest: /etc/ssh/sshd_config.d/00-remnawave.conf", system_tasks)
        self.assertIn("argv: [/usr/sbin/sshd, -T]", system_tasks)

    def test_vault_example_is_not_auto_loaded_as_real_group_vars(self) -> None:
        for inventory in ("staging", "production"):
            inventory_root = ROOT / "inventories" / inventory / "group_vars"
            self.assertTrue((inventory_root / "all" / "vault.yml.example").is_file())
            self.assertFalse((inventory_root / "vault.example.yml").exists())
            # Anything ending in .yml inside group_vars is loaded by Ansible, so an
            # example file must never use that extension: it would define real
            # variables and shadow whatever the operator did not override.
            for path in inventory_root.rglob("*.yml"):
                self.assertNotIn(
                    ".example", path.name, f"{path} would be auto-loaded by Ansible"
                )

    def test_ipaddr_preflight_conditions_return_booleans(self) -> None:
        preflight = self.read("roles/node_base/tasks/preflight.yml")
        self.assertNotIn("- node_public_ip | ansible.utils.ipaddr\n", preflight)
        self.assertNotIn("- item | ansible.utils.ipaddr\n", preflight)
        self.assertIn("(node_public_ip | ansible.utils.ipaddr) != false", preflight)

    def test_read_only_preflight_commands_run_in_check_mode(self) -> None:
        preflight = self.read("roles/node_base/tasks/preflight.yml")
        self.assertGreaterEqual(preflight.count("check_mode: false"), 4)

    def test_bios_grub_target_is_discovered_before_package_upgrade(self) -> None:
        system_tasks = self.read("roles/node_base/tasks/system.yml")
        self.assertIn("argv: [findmnt, -n, -o, SOURCE, /]", system_tasks)
        self.assertIn("question: grub-pc/install_devices", system_tasks)
        self.assertIn("argv: [dpkg, --configure, -a]", system_tasks)
        self.assertLess(
            system_tasks.index("question: grub-pc/install_devices"),
            system_tasks.index("Refresh APT metadata and upgrade installed packages"),
        )

    def test_reality_key_parser_accepts_current_xray_label(self) -> None:
        profile_tasks = self.read("roles/remnawave_panel/tasks/profile.yml")
        self.assertIn("Private\\s*key", profile_tasks)

    def test_firewall_requires_a_new_authenticated_connection(self) -> None:
        firewall = self.read("roles/node_base/tasks/firewall.yml")
        preflight = self.read("roles/node_base/tasks/preflight.yml")
        self.assertIn("ansible.builtin.meta: reset_connection", firewall)
        self.assertIn("ansible.builtin.wait_for_connection", firewall)
        self.assertNotIn("ansible.builtin.wait_for:\n", firewall)
        self.assertIn("node_base_observed_ssh_source", preflight)


class OperatorWorkflowTests(unittest.TestCase):
    """The cost of adding a node must stay at two lines and one command."""

    def read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_a_new_host_needs_only_address_and_label(self) -> None:
        hosts = yaml.safe_load(self.read("inventories/staging/hosts.yml"))
        block = hosts["all"]["children"]["remnawave_nodes"]["hosts"]["ee01"]
        self.assertEqual(set(block), {"ansible_host", "node_host_remark"})

    def test_identity_is_derived_from_the_inventory_hostname(self) -> None:
        for inventory in ("staging", "production"):
            group_vars = self.read(f"inventories/{inventory}/group_vars/remnawave_nodes.yml")
            self.assertIn("inventory_hostname | regex_replace", group_vars)
            self.assertIn('selfsteal_domain: "{{ inventory_hostname }}.{{ node_domain_zone }}"', group_vars)
            # Nothing identity-shaped may be required per host any more.
            self.assertNotIn("\nnode_id: ee_01", group_vars)

    def test_derivation_block_does_not_drift_between_inventories(self) -> None:
        def derivation(text: str) -> str:
            start = text.index("# --- Derived identity")
            end = text.index("# --- Runtime")
            return text[start:end]

        staging = derivation(self.read("inventories/staging/group_vars/remnawave_nodes.yml"))
        production = derivation(self.read("inventories/production/group_vars/remnawave_nodes.yml"))
        self.assertEqual(staging, production)

    def test_bootstrap_credentials_are_not_stored_in_inventory(self) -> None:
        defaults = self.read("roles/node_bootstrap/defaults/main.yml")
        # The root password is read from the environment (the wrapper prompts for
        # it) and only falls back to the vault; it never lands in inventory.
        self.assertIn("lookup('env', 'NODE_ROOT_PASSWORD')", defaults)
        self.assertIn("vault_node_root_password", defaults)
        self.assertIn("first_found", defaults)
        for inventory in ("staging", "production"):
            group_vars = self.read(f"inventories/{inventory}/group_vars/remnawave_nodes.yml")
            self.assertNotIn("bootstrap_ssh_password", group_vars)
            self.assertNotIn("bootstrap_authorized_keys", group_vars)

    def test_controller_address_is_discovered_not_configured(self) -> None:
        preflight = self.read("roles/node_base/tasks/preflight.yml")
        panel = self.read("inventories/staging/group_vars/all/panel.yml")
        self.assertIn("Resolve the management allow list", preflight)
        self.assertIn("management_cidrs_extra", preflight)
        self.assertIn("management_cidrs_extra: []", panel)
        # An explicitly configured list must still win, and still be validated.
        self.assertIn("Require the management allow list to include the source seen by sshd", preflight)

    def test_panel_state_is_checked_before_the_node_is_touched(self) -> None:
        preflight = self.read("roles/node_base/tasks/preflight.yml")
        main = self.read("roles/node_base/tasks/main.yml")
        # preflight is the first thing node_base does, and node_base is the first
        # mutating role, so these checks run while the server is still untouched.
        self.assertLess(main.index("preflight.yml"), main.index("system.yml"))
        for check in (
            "Reject a conflicting Node in the panel",
            "Require the shared Config Profile to exist before the node is touched",
            "Require the target Config Profile to carry routing rules",
            "Reject an inbound tag that another Config Profile already owns",
            "Reject ambiguous Hosts in the panel",
        ):
            self.assertIn(check, preflight)

    def test_wrappers_stay_thin(self) -> None:
        for name in ("provision-node", "setup-controller"):
            script = self.read(name)
            self.assertTrue(script.startswith("#!/usr/bin/env bash"))
            # Infrastructure logic belongs in the playbooks, not in the wrapper.
            for forbidden in ("nft ", "certbot", "docker run", "/api/nodes", "iptables"):
                self.assertNotIn(forbidden, script, f"{name} must not contain {forbidden!r}")
        wrapper = self.read("provision-node")
        self.assertIn("playbooks/provision_node.yml", wrapper)
        self.assertIn("NODE_ROOT_PASSWORD", wrapper)
        # The probe must not silently record trust in a new server's host key.
        self.assertIn("UserKnownHostsFile=/dev/null", wrapper)


if __name__ == "__main__":
    unittest.main()
