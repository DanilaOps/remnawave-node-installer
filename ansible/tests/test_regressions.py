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
        tasks = self.read("roles/remnawave_node/tasks/certificate.yml")
        self.assertNotIn("state: stopped", tasks)
        self.assertIn("remnawave_certificate_authenticator", tasks)
        # Standalone is only reachable when nginx is not running yet.
        self.assertIn(
            "{{ 'webroot'\n         if (remnawave_nginx_before_acme.container.State.Running",
            tasks,
        )

    def test_acme_environment_switch_forces_a_clean_reissue(self) -> None:
        defaults = yaml.safe_load(self.read("roles/remnawave_node/defaults/main.yml"))
        tasks = self.read("roles/remnawave_node/tasks/certificate.yml")
        self.assertEqual(defaults["certificate_acme_environment"], "production")
        self.assertIn("staging", defaults["certificate_acme_directories"])
        self.assertIn("acme-staging-v02", defaults["certificate_acme_directories"]["staging"])
        self.assertIn("remnawave_certificate_environment_mismatch", tasks)
        self.assertIn("--force-renewal", tasks)
        self.assertIn("Require the installed certificate to match", tasks)


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


if __name__ == "__main__":
    unittest.main()
