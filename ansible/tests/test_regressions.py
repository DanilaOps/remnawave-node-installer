from __future__ import annotations

import pathlib
import unittest

import yaml


ROOT = pathlib.Path(__file__).parents[1]
REPO = ROOT.parent
GROUP_VARS = ROOT / "playbooks" / "group_vars"


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
        template = self.read("roles/node_base/templates/00-remnawave-ssh.conf.j2")
        system_tasks = self.read("roles/node_base/tasks/system.yml")
        fleet = yaml.safe_load(self.read("playbooks/group_vars/remnawave_nodes/fleet.yml"))
        # The role default stays the safe one for anybody else using these
        # roles; this fleet opts in explicitly, and the run proves the
        # effective policy afterwards instead of trusting the rendered file.
        self.assertFalse(defaults["node_ssh_allow_root_password"])
        self.assertTrue(fleet["node_ssh_allow_root_password"])
        self.assertIn("node_ssh_allow_root_password", template)
        # 00- so the drop-in wins: sshd takes the first value it sees, and a
        # vendor or cloud-init file sorting earlier would silently override us.
        self.assertIn("dest: /etc/ssh/sshd_config.d/00-remnawave.conf", system_tasks)
        self.assertIn("path: /etc/ssh/sshd_config.d/90-remnawave.conf", system_tasks)
        self.assertIn("argv: [/usr/sbin/sshd, -t]", system_tasks)
        self.assertIn("argv: [/usr/sbin/sshd, -T]", system_tasks)
        self.assertLess(
            system_tasks.index("argv: [/usr/sbin/sshd, -t]"),
            system_tasks.index("argv: [/usr/sbin/sshd, -T]"),
        )

    def test_examples_live_outside_every_group_vars_directory(self) -> None:
        # Ansible loads every .yml it finds in a group_vars directory, so an
        # example kept there defines real variable names the moment somebody
        # drops the .example suffix. Examples live in ansible/examples/ instead.
        self.assertTrue((ROOT / "examples" / "secrets.yml.example").is_file())
        self.assertTrue((ROOT / "examples" / "fleet.yml.example").is_file())
        for path in GROUP_VARS.rglob("*"):
            if path.is_file():
                self.assertNotIn(
                    ".example", path.name, f"{path} would be auto-loaded by Ansible"
                )
        self.assertEqual(
            [], list((ROOT / "inventories").rglob("group_vars")),
            "fleet configuration must live next to the playbook, not next to an inventory",
        )

    def test_ipaddr_preflight_conditions_return_booleans(self) -> None:
        preflight = "".join(
            self.read(f"roles/node_base/tasks/{name}.yml")
            for name in ("preflight_controller", "preflight_node", "preflight_management_cidrs")
        )
        self.assertNotIn("- node_public_ip | ansible.utils.ipaddr\n", preflight)
        self.assertNotIn("- item | ansible.utils.ipaddr\n", preflight)
        self.assertIn("(node_public_ip | ansible.utils.ipaddr) != false", preflight)

    def test_read_only_preflight_commands_run_in_check_mode(self) -> None:
        controller = self.read("roles/node_base/tasks/preflight_controller.yml")
        node = self.read("roles/node_base/tasks/preflight_node.yml")
        # Every read the checks depend on has to happen for real, or --check
        # reports a green preflight it never performed.
        self.assertGreaterEqual(controller.count("check_mode: false"), 5)
        self.assertGreaterEqual(node.count("check_mode: false"), 3)

    def test_management_allow_list_is_deduplicated(self) -> None:
        cidrs = self.read("roles/node_base/tasks/preflight_management_cidrs.yml")
        # | unique binds tighter than +, so it has to wrap the whole sum. The
        # behaviour itself is covered by tests/management_cidrs.yml.
        self.assertIn("+ (management_cidrs_extra | default([]))) | unique }}", cidrs)
        self.assertNotIn("+ (management_cidrs_extra | default([])) | unique }}", cidrs)

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
        preflight = self.read("roles/node_base/tasks/preflight_node.yml")
        self.assertIn("ansible.builtin.meta: reset_connection", firewall)
        self.assertIn("ansible.builtin.wait_for_connection", firewall)
        self.assertNotIn("ansible.builtin.wait_for:\n", firewall)
        self.assertIn("node_base_observed_ssh_source", preflight)


class OperatorWorkflowTests(unittest.TestCase):
    """The cost of adding a node must stay at two lines and one command."""

    def read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_a_new_host_needs_only_an_address(self) -> None:
        hosts = yaml.safe_load(self.read("inventories/staging/hosts.yml"))
        block = hosts["all"]["children"]["remnawave_nodes"]["hosts"]["ee01"]
        self.assertEqual(set(block), {"ansible_host"})

    def test_identity_is_derived_from_the_inventory_hostname(self) -> None:
        identity = self.read("playbooks/group_vars/remnawave_nodes/identity.yml")
        self.assertIn("inventory_hostname | regex_replace", identity)
        self.assertIn(
            'selfsteal_domain: "{{ inventory_hostname }}.{{ node_domain_zone }}"', identity
        )
        # Nothing identity-shaped may be required per host any more.
        self.assertNotIn("\nnode_id: ee_01", identity)

    def test_fleet_configuration_is_declared_exactly_once(self) -> None:
        # Two copies of the same fleet file under two inventories used to be kept
        # in step by a test. The structure now makes the drift impossible: the
        # files live next to the playbook and every inventory shares them.
        self.assertTrue((GROUP_VARS / "all" / "panel.yml").is_file())
        self.assertTrue((GROUP_VARS / "all" / "countries.yml").is_file())
        self.assertTrue((GROUP_VARS / "remnawave_nodes" / "identity.yml").is_file())
        self.assertTrue((GROUP_VARS / "remnawave_nodes" / "fleet.yml").is_file())
        inventories = [
            path for path in (ROOT / "inventories").rglob("*.yml") if path.is_file()
        ]
        self.assertTrue(inventories)
        for path in inventories:
            self.assertEqual(
                "hosts.yml", path.name,
                f"{path} makes an inventory carry more than node addresses",
            )

    def test_connection_variables_stay_scoped_to_the_node_group(self) -> None:
        # group_vars/all applies to every host a playbook in this directory
        # targets, localhost and the controller included. ansible_user there
        # would silently redirect a localhost play to the deployer account.
        for path in (GROUP_VARS / "all").glob("*.yml"):
            values = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for connection_var in ("ansible_user", "ansible_port", "ansible_host"):
                self.assertNotIn(connection_var, values, f"{path} must not set {connection_var}")
        fleet = yaml.safe_load(self.read("playbooks/group_vars/remnawave_nodes/fleet.yml"))
        self.assertEqual("deployer", fleet["ansible_user"])

    def test_real_values_reach_a_semaphore_job_and_the_cli_alike(self) -> None:
        # Semaphore clones the repository for every job, so a git-ignored file
        # inside the checkout never reaches a run started from the UI. The real
        # values therefore live on the controller and are loaded as extra-vars
        # files - by the wrapper, and by name in every template's arguments.
        self.assertTrue((ROOT / "examples" / "fleet.yml.example").is_file())
        self.assertTrue((ROOT / "examples" / "secrets.yml.example").is_file())
        wrapper = (REPO / "provision-node").read_text(encoding="utf-8")
        self.assertIn('REMNAWAVE_FLEET_VARS:-/etc/remnawave/fleet.yml', wrapper)
        self.assertIn('value_args+=(-e "@$file")', wrapper)
        tool = self.read("tools/semaphore_bootstrap.py")
        self.assertIn('["-e", f"@{arguments.fleet_values}", "-e", f"@{arguments.secret_values}"]', tool)
        # The vault password is a path in the variable group, never a value.
        self.assertIn('"ANSIBLE_VAULT_PASSWORD_FILE": arguments.vault_password_file', tool)
        self.assertIn('"json": "{}"', tool)

    def test_the_published_documentation_values_cannot_reach_a_node(self) -> None:
        # Everything tracked here is an example value. A run that still sees them
        # lost its deployment values, and publishing a node against a panel that
        # does not exist is worse than stopping.
        preflight = self.read("roles/node_base/tasks/preflight_controller.yml")
        panel = yaml.safe_load(self.read("playbooks/group_vars/all/panel.yml"))
        self.assertEqual("https://panel.example.com", panel["remnawave_panel_url"])
        self.assertIn("Refuse to run against the published documentation values", preflight)
        self.assertIn("remnawave_panel_url != 'https://panel.example.com'", preflight)
        self.assertIn("node_domain_zone != 'example.com'", preflight)

    def test_a_fleet_wide_identity_cannot_be_smuggled_in(self) -> None:
        # Extra vars outrank every file and every host, so one node_id supplied
        # globally - a Semaphore variable group, a stray -e - would give the whole
        # fleet one identity and make each run overwrite the previous node.
        preflight = self.read("roles/node_base/tasks/preflight_controller.yml")
        defaults = yaml.safe_load(self.read("roles/node_base/defaults/main.yml"))
        self.assertIn("Refuse a node identity that did not come from its inventory hostname", preflight)
        self.assertIn("node_public_ip == ansible_host", preflight)
        self.assertIn("selfsteal_domain == inventory_hostname ~ '.' ~ node_domain_zone", preflight)
        self.assertFalse(defaults["node_identity_override_ack"])

    def test_bootstrap_credentials_are_not_stored_in_inventory(self) -> None:
        defaults = self.read("roles/node_bootstrap/defaults/main.yml")
        # The root password is read from the environment (the wrapper prompts for
        # it, Semaphore passes a secret survey field of the same name) and only
        # falls back to the vault; it never lands in inventory or group_vars.
        self.assertIn("lookup('env', 'NODE_ROOT_PASSWORD')", defaults)
        self.assertIn("vault_node_root_password", defaults)
        self.assertIn("first_found", defaults)
        for path in list(GROUP_VARS.rglob("*.yml")) + list((ROOT / "inventories").rglob("*.yml")):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("bootstrap_ssh_password", text, f"{path} must not carry a password")
            self.assertNotIn("bootstrap_authorized_keys", text)

    def test_controller_address_is_discovered_not_configured(self) -> None:
        cidrs = self.read("roles/node_base/tasks/preflight_management_cidrs.yml")
        panel = self.read("playbooks/group_vars/all/panel.yml")
        self.assertIn("Resolve the management allow list", cidrs)
        self.assertIn("management_cidrs_extra", cidrs)
        # No real address is published: the controller's own is discovered from
        # the live SSH session, and a workstation goes into the ignored override.
        self.assertIn("management_cidrs_extra: []", panel)
        # An explicitly configured list must still win, and still be validated.
        self.assertIn("Require the management allow list to include the source seen by sshd", cidrs)

    def test_panel_state_is_checked_before_anything_changes(self) -> None:
        preflight = self.read("roles/node_base/tasks/preflight_controller.yml")
        plays = yaml.safe_load(self.read("playbooks/install_node.yml"))
        # The panel checks are their own play, before the play that reconciles
        # DNS and before the play that touches the server: a conflict has to stop
        # the run while the registrar and the node are both untouched.
        self.assertEqual(3, len(plays))
        self.assertIn("preflight_controller.yml", self.read("playbooks/install_node.yml"))
        self.assertLess(
            self.read("playbooks/install_node.yml").index("preflight_controller.yml"),
            self.read("playbooks/install_node.yml").index("role: dns"),
        )
        self.assertEqual("local", plays[0]["connection"])
        self.assertEqual("local", plays[1]["connection"])
        self.assertNotIn("connection", plays[2])
        # One node per run, and a fleet-wide reconcile still one at a time.
        self.assertEqual(1, plays[1]["serial"])
        self.assertEqual(1, plays[2]["serial"])
        self.assertIn("node_allow_bulk", self.read("playbooks/install_node.yml"))
        # The node-side half still runs before the first change to the server.
        main = self.read("roles/node_base/tasks/main.yml")
        self.assertLess(main.index("preflight_node.yml"), main.index("system.yml"))
        for check in (
            "Reject a conflicting Node in the panel",
            "Require the shared Config Profile to exist before the node is touched",
            "Require the target Config Profile to carry routing rules",
            "Reject an inbound tag that another Config Profile already owns",
            "Reject ambiguous Hosts in the panel",
            "Treat an empty Config Profile list as a panel problem",
            "Require a display name for this node's country",
            "Require an unmanaged DNS record to already be correct",
            "Require a panel token before anything asks the panel for something",
        ):
            self.assertIn(check, preflight)


class SharedProfileTests(unittest.TestCase):
    """The shared profile carries the routing every published Host depends on."""

    def read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_the_profile_is_re_read_immediately_before_it_is_written(self) -> None:
        profile = self.read("roles/remnawave_panel/tasks/profile.yml")
        recheck = profile.index("Re-read the shared Config Profile immediately before writing it")
        guard = profile.index("Refuse to overwrite a Config Profile that changed")
        patch = profile.index("Update Config Profile only when its managed config differs")
        # Read, compare, then write - in that order, with nothing in between.
        self.assertLess(recheck, guard)
        self.assertLess(guard, patch)
        self.assertIn("remnawave_profile_fingerprint_before", profile)
        self.assertIn("to_json(sort_keys=True) | hash('sha256')", profile)

    def test_nothing_that_was_in_the_profile_may_disappear(self) -> None:
        profile = self.read("roles/remnawave_panel/tasks/profile.yml")
        self.assertIn("remnawave_profile_tags_before", profile)
        survival = profile.index("Require every inbound the shared profile already held")
        final_read = profile.index("Read reconciled Config Profile and panel-assigned inbound UUIDs")
        self.assertLess(final_read, survival)

    def test_pruning_is_limited_to_this_node_namespace(self) -> None:
        profile = self.read("roles/remnawave_panel/tasks/profile.yml")
        self.assertIn("Require every pruned inbound tag to belong to this node", profile)
        # The check must come before the first request, not after the merge.
        self.assertLess(
            profile.index("Require every pruned inbound tag to belong to this node"),
            profile.index("List Config Profiles"),
        )

    def test_only_protocols_the_panel_strips_per_node_are_allowed(self) -> None:
        # The panel filters inbounds of managed protocols a node does not
        # activate, so a neighbour's Reality key never reaches it. An inbound of
        # any other protocol goes to every node in full.
        defaults = yaml.safe_load(self.read("roles/remnawave_panel/defaults/main.yml"))
        self.assertEqual(
            ["vless", "trojan", "shadowsocks", "hysteria"],
            defaults["shared_profile_managed_protocols"],
        )
        self.assertEqual([], defaults["shared_profile_allowed_unmanaged_inbound_tags"])
        profile = self.read("roles/remnawave_panel/tasks/profile.yml")
        self.assertIn("shared_profile_allowed_unmanaged_inbound_tags", profile)

    def test_the_whole_profile_never_stays_on_a_node_after_a_failure(self) -> None:
        profile = self.read("roles/remnawave_panel/tasks/profile.yml")
        block = profile.index("Validate the merged profile with the pinned Node image")
        always = profile.index("always:", block)
        removal = profile.index("Remove the temporary validation profile", block)
        self.assertLess(always, removal)

    def test_adoption_requires_an_explicit_uuid(self) -> None:
        profile = self.read("roles/remnawave_panel/tasks/profile.yml")
        self.assertIn("remnawave_adopt_existing_profile | bool and profile_uuid | length > 0", profile)

    def test_wrappers_stay_thin(self) -> None:
        for name in ("provision-node", "setup-controller"):
            script = (REPO / name).read_text(encoding="utf-8")
            self.assertTrue(script.startswith("#!/usr/bin/env bash"))
            # Infrastructure logic belongs in the playbooks, not in the wrapper.
            for forbidden in ("nft ", "certbot", "docker run", "/api/nodes", "iptables"):
                self.assertNotIn(forbidden, script, f"{name} must not contain {forbidden!r}")
        wrapper = (REPO / "provision-node").read_text(encoding="utf-8")
        self.assertIn("ansible/playbooks/provision_node.yml", wrapper)
        self.assertIn("NODE_ROOT_PASSWORD", wrapper)
        # The probe must not silently record trust in a new server's host key.
        self.assertIn("UserKnownHostsFile=/dev/null", wrapper)

    def test_one_configuration_and_one_entry_point(self) -> None:
        config = (REPO / "ansible.cfg").read_text(encoding="utf-8")
        unit = self.read("roles/semaphore_controller/templates/semaphore.service.j2")
        self.assertIn("roles_path = ansible/roles", config)
        self.assertIn("filter_plugins = ansible/filter_plugins", config)
        # Collections belong to the checkout, not to the invoking user's home:
        # the service user runs with ProtectHome and would not see them there.
        self.assertIn("collections_path = ansible/collections", config)
        # A second configuration file is how the CLI and the UI drift apart.
        self.assertFalse(
            (ROOT / "ansible.cfg").exists(), "ansible/ansible.cfg must not come back"
        )
        for name in ("provision-node", "setup-controller"):
            self.assertTrue((REPO / name).is_file(), f"{name} must live in the repository root")
            self.assertFalse((ROOT / name).exists(), f"{name} must not be duplicated under ansible/")
        self.assertIn("Environment=SEMAPHORE_INTERFACE={{ semaphore_interface }}", unit)
        self.assertIn("Environment=SEMAPHORE_HOME_DIR_MODE=user_home", unit)
        # A persistent HOME is what makes a node's accepted host key survive.
        self.assertIn("Environment=HOME={{ semaphore_home }}", unit)
        # The unit must not carry a configuration path of its own: one ansible.cfg.
        self.assertNotIn("ANSIBLE_CONFIG", unit)
        jail = self.read("roles/semaphore_controller/templates/fail2ban-sshd.local.j2")
        self.assertIn("backend = systemd", jail)
        # The unit and the jail are templates of the controller role now, so
        # there is exactly one owner for each file on the controller.
        self.assertFalse(
            (ROOT / "semaphore" / "semaphore.service").exists(),
            "the unit has one owner: the controller role",
        )


class ControllerTests(unittest.TestCase):
    """The controller is rebuildable and cannot publish its own UI."""

    def read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_semaphore_version_is_pinned_and_verified(self) -> None:
        defaults = yaml.safe_load(self.read("roles/semaphore_controller/defaults/main.yml"))
        self.assertEqual("2.18.29", defaults["semaphore_version"])
        tasks = self.read("roles/semaphore_controller/tasks/semaphore.yml")
        # Downloaded against a checksum, and the running binary is checked
        # against the pin afterwards - a controller that cannot be rebuilt to the
        # same version is not rebuildable.
        self.assertIn("checksum:", tasks)
        self.assertIn("Require the installed binary to be the pinned version", tasks)

    def test_the_declared_service_command_exists_in_the_pinned_cli(self) -> None:
        unit = self.read("roles/semaphore_controller/templates/semaphore.service.j2")
        self.assertIn(" server --config=", unit)
        self.assertNotIn(" service --config", unit)

    def test_sqlite_is_a_supported_existing_database(self) -> None:
        defaults = yaml.safe_load(self.read("roles/semaphore_controller/defaults/main.yml"))
        preflight = self.read("roles/semaphore_controller/tasks/preflight.yml")
        config = self.read("roles/semaphore_controller/templates/semaphore-config.json.j2")
        self.assertIn("sqlite3", defaults["controller_packages"])
        self.assertIn("'sqlite'", preflight)
        self.assertIn('"sqlite":', config)

    def test_encryption_keys_are_generated_once_and_reused(self) -> None:
        preflight = self.read("roles/semaphore_controller/tasks/preflight.yml")
        tasks = self.read("roles/semaphore_controller/tasks/semaphore.yml")
        # New keys would make every stored Key Store entry undecryptable.
        self.assertIn("Read an existing Semaphore configuration", preflight)
        self.assertIn("semaphore_existing_config[item] | default('') | length == 0", tasks)

    def test_the_database_dialect_is_never_migrated_silently(self) -> None:
        preflight = self.read("roles/semaphore_controller/tasks/preflight.yml")
        self.assertIn("Refuse to change the database dialect of an existing instance", preflight)

    def test_the_ui_cannot_be_bound_beyond_loopback(self) -> None:
        preflight = self.read("roles/semaphore_controller/tasks/preflight.yml")
        tasks = self.read("roles/semaphore_controller/tasks/semaphore.yml")
        self.assertIn("Refuse to expose the Semaphore UI beyond loopback", preflight)
        self.assertIn("Require the UI listener to be loopback only", tasks)

    def test_the_controller_never_installs_or_uses_docker(self) -> None:
        # The controller runs playbooks; it does not run workloads. Saying so in
        # a comment is allowed, doing it is not.
        for path in (ROOT / "roles" / "semaphore_controller").rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            for forbidden in (
                # A module invocation, not the collection name: the controller
                # installs community.docker because the node roles import it, and
                # verifies the service user can see it. It never calls it.
                "community.docker.",
                "docker run",
                "docker compose",
                "docker.io",
                "docker-ce",
            ):
                self.assertNotIn(forbidden, text, f"{path} brings Docker onto the controller")
        defaults = yaml.safe_load(self.read("roles/semaphore_controller/defaults/main.yml"))
        self.assertNotIn("docker", " ".join(defaults["controller_packages"]))

    def test_the_controller_installs_the_pinned_xray_probe_client(self) -> None:
        defaults = yaml.safe_load(self.read("roles/semaphore_controller/defaults/main.yml"))
        runtime = self.read("roles/semaphore_controller/tasks/runtime.yml")
        self.assertEqual("26.6.27", defaults["controller_xray_version"])
        self.assertEqual("/usr/local/bin/xray", defaults["controller_xray_binary_path"])
        self.assertIn("Download the pinned controller Xray archive", runtime)
        self.assertIn("controller_xray_checksums", runtime)
        self.assertIn("Require the installed controller Xray client to match the pin", runtime)

    def test_collection_visibility_does_not_require_sudo_on_debian(self) -> None:
        runtime = self.read("roles/semaphore_controller/tasks/runtime.yml")
        check = runtime.split("- name: Prove the service user can resolve", 1)[1].split(
            "- name: Require every collection", 1
        )[0]
        self.assertIn("- runuser", check)
        self.assertNotIn("become_user", check)

    def test_controller_xray_extraction_is_safe_in_check_mode(self) -> None:
        runtime = self.read("roles/semaphore_controller/tasks/runtime.yml")
        extract = runtime.split("- name: Extract the pinned controller Xray client", 1)[1].split(
            "- name: Publish the pinned controller Xray client", 1
        )[0]
        self.assertIn("not ansible_check_mode", extract)
        publish = runtime.split("- name: Publish the pinned controller Xray client", 1)[1].split(
            "- name: Read the installed controller Xray version", 1
        )[0]
        self.assertIn("not ansible_check_mode", publish)

    def test_the_controller_does_not_touch_ssh_authentication(self) -> None:
        # The controller is the machine an operator must be able to get back
        # into; this role is never the reason they cannot.
        for path in (ROOT / "roles" / "semaphore_controller").rglob("*"):
            if path.is_file():
                text = path.read_text(encoding="utf-8")
                for forbidden in ("PermitRootLogin", "PasswordAuthentication", "sshd_config"):
                    self.assertNotIn(forbidden, text, f"{path} changes SSH policy")

    def test_controller_dry_run_does_not_chmod_an_uncreated_swap_file(self) -> None:
        system = self.read("roles/semaphore_controller/tasks/system.yml")
        restrict = system.split("- name: Restrict the swap file", 1)[1].split(
            "- name: Format the swap file", 1
        )[0]
        self.assertIn("not ansible_check_mode", restrict)

    def test_the_firewall_cannot_lock_out_the_live_session(self) -> None:
        firewall = self.read("roles/semaphore_controller/tasks/firewall.yml")
        self.assertIn("controller_observed_ssh_source", firewall)
        self.assertIn("wait_for_connection", firewall)
        self.assertIn("rollback", firewall)

    def test_a_local_only_backup_is_reported_as_a_failure(self) -> None:
        script = self.read("roles/semaphore_controller/templates/semaphore-backup.sh.j2")
        # The Semaphore inventory is the node registry and exists nowhere else,
        # so an archive that never leaves this host is not a backup.
        self.assertIn("controller_backup_remote is not set", script)
        self.assertIn("exit 1", script)
        # And the keys that would decrypt it never travel with it.
        self.assertIn("excluded=encryption_keys,vault_password,deployer_private_key", script)

    def test_new_backup_units_do_not_break_controller_check_mode(self) -> None:
        backup = self.read("roles/semaphore_controller/tasks/backup.yml")
        enable = backup.split("- name: Enable the nightly backup timer", 1)[1].split(
            "- name: Say plainly", 1
        )[0]
        self.assertIn("not ansible_check_mode", enable)

    def test_semaphore_templates_enforce_serial_installs_and_secret_bootstrap(self) -> None:
        tool = self.read("tools/semaphore_bootstrap.py")
        self.assertIn('"allow_parallel_tasks": wanted["allow_parallel_tasks"]', tool)
        self.assertIn('"name": "bootstrap_ssh_password"', tool)
        self.assertIn('"type": "secret"', tool)
        self.assertIn('"name": "bootstrap_trust_new_host_keys"', tool)


if __name__ == "__main__":
    unittest.main()


class PublishedUiTests(unittest.TestCase):
    """Publishing the panel that provisions the fleet is the riskiest change here."""

    def read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_semaphore_itself_never_leaves_loopback(self) -> None:
        # The public listener is nginx. Semaphore keeps binding 127.0.0.1, so a
        # mistake in the proxy cannot expose the admin panel directly.
        defaults = yaml.safe_load(self.read("roles/semaphore_controller/defaults/main.yml"))
        self.assertEqual("127.0.0.1", defaults["semaphore_interface"])
        preflight = self.read("roles/semaphore_controller/tasks/preflight.yml")
        self.assertIn("Refuse to expose the Semaphore UI beyond loopback", preflight)
        proxy = self.read("roles/semaphore_controller/tasks/proxy.yml")
        self.assertIn("Require 80 and 443 to be public and the UI port not to be", proxy)

    def test_publishing_to_the_internet_needs_an_acknowledgement(self) -> None:
        defaults = yaml.safe_load(self.read("roles/semaphore_controller/defaults/main.yml"))
        preflight = self.read("roles/semaphore_controller/tasks/preflight.yml")
        self.assertFalse(defaults["controller_proxy_enabled"])
        self.assertEqual([], defaults["controller_proxy_allowed_cidrs"])
        self.assertFalse(defaults["controller_proxy_ack_public"])
        self.assertIn("Require an explicit decision before the UI is open to the internet", preflight)

    def test_unauthenticated_semaphore_routes_are_blocked_by_the_proxy(self) -> None:
        # Verified against Semaphore 2.18.29: /api/integrations/{alias},
        # /api/terraform/{alias} and /api/internal/runners are served with no
        # authentication. Harmless on loopback; published, a guessed alias runs a
        # task against the fleet.
        defaults = yaml.safe_load(self.read("roles/semaphore_controller/defaults/main.yml"))
        self.assertEqual(
            ["/api/integrations/", "/api/terraform/", "/api/internal/"],
            defaults["controller_proxy_blocked_paths"],
        )
        site = self.read("roles/semaphore_controller/templates/semaphore-nginx.conf.j2")
        self.assertIn("controller_proxy_blocked_paths", site)
        self.assertIn("return 404;", site)

    def test_live_task_output_survives_the_proxy(self) -> None:
        # The WebSocket is what shows a running task. Without the upgrade headers
        # the UI loads and then silently shows nothing.
        site = self.read("roles/semaphore_controller/templates/semaphore-nginx.conf.j2")
        self.assertIn("proxy_set_header Upgrade $http_upgrade;", site)
        self.assertIn("proxy_set_header Connection $connection_upgrade;", site)
        self.assertIn("proxy_read_timeout 3600s;", site)
        # $connection_upgrade is a map, and a map belongs in the http context.
        self.assertTrue(
            (ROOT / "roles/semaphore_controller/templates/nginx-upgrade-map.conf.j2").is_file()
        )

    def test_basic_auth_refuses_plaintext_passwords(self) -> None:
        preflight = self.read("roles/semaphore_controller/tasks/preflight.yml")
        self.assertIn("controller_proxy_basic_auth_users entries need a user and a password *hash*", preflight)

    def test_the_firewall_publishes_only_80_and_443(self) -> None:
        firewall = self.read("roles/semaphore_controller/templates/remnawave-controller.nft.j2")
        self.assertIn("tcp dport { 80, 443 } accept", firewall)
        self.assertNotIn("dport 3000", firewall)
        self.assertNotIn("dport { 80, 443, 3000 }", firewall)

    def test_accounts_exist_only_because_an_admin_made_them(self) -> None:
        # Semaphore 2.18.29 has no self-registration route at all, so there is no
        # setting to switch off. What is left is that a non-admin must not be able
        # to create projects, and that password login stays usable.
        defaults = yaml.safe_load(self.read("roles/semaphore_controller/defaults/main.yml"))
        config = self.read("roles/semaphore_controller/templates/semaphore-config.json.j2")
        self.assertFalse(defaults["semaphore_non_admin_can_create_project"])
        self.assertFalse(defaults["semaphore_password_login_disable"])
        self.assertIn('"non_admin_can_create_project"', config)
        self.assertIn('"password_login_disable"', config)

    def test_a_second_operator_is_not_an_admin(self) -> None:
        tool = self.read("tools/semaphore_bootstrap.py")
        self.assertIn('"admin": False', tool)
        self.assertIn('default="task_runner"', tool)
        self.assertIn("SEMAPHORE_NEW_USER_PASSWORD", tool)
        # The password is read from the environment for one command, never stored.
        self.assertNotIn("password=", tool.replace('"password": password', ""))

    def test_the_public_url_is_what_semaphore_believes_it_is(self) -> None:
        # Semaphore builds links and cookie scope from web_host; left pointing at
        # loopback, a published instance redirects people to 127.0.0.1.
        defaults = self.read("roles/semaphore_controller/defaults/main.yml")
        config = self.read("roles/semaphore_controller/templates/semaphore-config.json.j2")
        self.assertIn("semaphore_web_host:", defaults)
        self.assertIn("'https://' ~ controller_proxy_domain", defaults)
        self.assertIn('"web_host": "{{ semaphore_web_host | trim }}"', config)


class ValidateArgumentTests(unittest.TestCase):
    """template/copy validate must contain %s, and must fit what it validates."""

    def test_every_validate_names_the_temporary_file(self) -> None:
        # Ansible refuses a validate command without %s, and it refuses it at
        # runtime on the target - so a missing %s is a task that only ever fails
        # on a real host, which is exactly what happened once.
        offenders = []
        for path in (ROOT / "roles").rglob("tasks/*.yml"):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("validate:") and "%s" not in stripped:
                    offenders.append(f"{path.relative_to(ROOT)}:{number}: {stripped}")
        self.assertEqual([], offenders, "validate without %s: " + "; ".join(offenders))

    def test_the_nginx_site_is_checked_as_part_of_the_whole_config(self) -> None:
        # A site file is a fragment: its server blocks are only valid inside
        # http {}, so it cannot be validated on its own. The whole configuration
        # is checked instead, before nginx is started or reloaded.
        proxy = (ROOT / "roles/semaphore_controller/tasks/proxy.yml").read_text(encoding="utf-8")
        self.assertIn("argv: [nginx, -t]", proxy)
        self.assertLess(
            proxy.index("argv: [nginx, -t]"),
            proxy.index("Start nginx so the challenge path is served"),
        )
        self.assertIn("Validate the complete nginx configuration with TLS enabled", proxy)


class ProxyCertificateTests(unittest.TestCase):
    """staging -> production has to actually replace the certificate."""

    def read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_the_environment_switch_forces_a_reissue(self) -> None:
        # A staging certificate is still valid, so --keep-until-expiring keeps it
        # and the issuer assertion then fails on every run. The environment of
        # what is installed decides, not the calendar.
        proxy = self.read("roles/semaphore_controller/tasks/proxy.yml")
        self.assertIn("controller_proxy_certificate_is_staging", proxy)
        self.assertIn(
            "\'--force-renewal\' if controller_proxy_force_reissue | bool "
            "else \'--keep-until-expiring\'",
            proxy,
        )
        # The decision is made before certbot runs, from the installed issuer.
        self.assertLess(
            proxy.index("Plan the issuance"),
            proxy.index("- name: Issue the certificate"),
        )
        self.assertLess(
            proxy.index("Inspect the certificate that is already installed"),
            proxy.index("Plan the issuance"),
        )

    def test_a_run_that_changes_nothing_does_not_spend_an_issuance(self) -> None:
        proxy = self.read("roles/semaphore_controller/tasks/proxy.yml")
        self.assertIn("--keep-until-expiring", proxy)
        self.assertIn(
            "changed_when: \"'Certificate not yet due for renewal' not in "
            "controller_proxy_certbot.stdout\"",
            proxy,
        )

    def test_a_replaced_certificate_is_followed_by_a_reload(self) -> None:
        # No file this role writes changes when only the lineage is replaced, so
        # without this nginx keeps serving the old certificate until something
        # else reloads it.
        proxy = self.read("roles/semaphore_controller/tasks/proxy.yml")
        certbot = proxy.index("- name: Issue the certificate")
        following = proxy[certbot:proxy.index("- name: Confirm the certificate is now installed")]
        self.assertIn("notify: Reload nginx", following)


class DryRunTests(unittest.TestCase):
    """A --check run must be honest: real reads, no faked verification, no crash.

    Every case here was a run that either failed with an undefined attribute or
    reported a check it could not have made.
    """

    def read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def task(self, relative: str, name: str) -> dict:
        for entry in yaml.safe_load(self.read(relative)):
            if isinstance(entry, dict) and entry.get("name") == name:
                return entry
        self.fail(f"{relative} has no task named {name!r}")

    def test_platform_check_accepts_ubuntu_2404(self) -> None:
        # ansible_facts.distribution_major_version is "24" on Ubuntu 24.04, so
        # comparing it with the documented "24.04" rejected a supported platform.
        import jinja2

        condition = self.task(
            "roles/node_base/tasks/preflight_node.yml", "Validate supported platform"
        )["ansible.builtin.assert"]["that"]
        defaults = yaml.safe_load(self.read("roles/node_base/defaults/main.yml"))
        environment = jinja2.Environment(undefined=jinja2.StrictUndefined)

        def accepts(distribution: str, major: str, version: str) -> bool:
            context = {
                "ansible_facts": {
                    "distribution": distribution,
                    "distribution_major_version": major,
                    "distribution_version": version,
                    "architecture": "x86_64",
                },
                "node_base_supported_os": defaults["node_base_supported_os"],
                "node_base_supported_architectures": defaults[
                    "node_base_supported_architectures"
                ],
            }
            return all(
                environment.from_string("{{ (" + test + ") }}").render(context) == "True"
                for test in condition
            )

        self.assertTrue(accepts("Ubuntu", "24", "24.04"))
        self.assertTrue(accepts("Debian", "12", "12.11"))
        self.assertTrue(accepts("Debian", "13", "13.0"))
        self.assertFalse(accepts("Ubuntu", "22", "22.04"))
        self.assertFalse(accepts("Ubuntu", "24", "24.10"))
        self.assertFalse(accepts("Debian", "11", "11.9"))

    def test_panel_reads_run_in_check_mode(self) -> None:
        # Every one of these is a GET. Skipping them in check mode left the next
        # task dereferencing an empty result.
        for relative, name in [
            ("roles/remnawave_panel/tasks/authenticate.yml", "Validate Panel API token"),
            ("roles/remnawave_panel/tasks/authenticate.yml", "Request RemnaNode SECRET_KEY"),
            ("roles/remnawave_panel/tasks/profile.yml", "List Config Profiles"),
            (
                "roles/remnawave_panel/tasks/profile.yml",
                "Read reconciled Config Profile and panel-assigned inbound UUIDs",
            ),
            ("roles/remnawave_panel/tasks/node.yml", "List Nodes"),
            ("roles/remnawave_panel/tasks/node.yml", "Read reconciled Node"),
            ("roles/remnawave_panel/tasks/hosts.yml", "List existing Hosts"),
            ("roles/remnawave_panel/tasks/hosts.yml", "Re-read Hosts after reconciliation"),
            ("roles/remnawave_panel/tasks/squad.yml", "List Internal Squads"),
            ("roles/node_verify/tasks/panel.yml", "Wait until Panel reports Node online"),
        ]:
            with self.subTest(task=name):
                self.assertIs(self.task(relative, name).get("check_mode"), False)

    def test_generated_secrets_are_produced_in_check_mode_too(self) -> None:
        # Generating a random string changes nothing, so a dry-run may do it and
        # then validate what it built. Both used to be skipped, leaving the
        # consumer with an undefined stdout.
        for relative, name in [
            (
                "roles/remnawave_panel/tasks/bridge_identity.yml",
                "Generate a new bridge secret only for a new service user",
            ),
            (
                "roles/remnawave_panel/tasks/profile.yml",
                "Generate a Reality short ID when none can be reused",
            ),
        ]:
            with self.subTest(task=name):
                task = self.task(relative, name)
                self.assertIs(task.get("check_mode"), False)
                # The Panel write below is the change, not the generation.
                self.assertIs(task.get("changed_when"), False)

    def test_object_uuids_prefer_what_the_panel_already_has(self) -> None:
        # The POST is skipped in check mode, so reading its response first meant
        # falling back to matches[0] of an empty list.
        for relative in [
            "roles/remnawave_panel/tasks/node.yml",
            "roles/remnawave_panel/tasks/squad.yml",
            "roles/remnawave_panel/tasks/host_item.yml",
        ]:
            with self.subTest(file=relative):
                text = self.read(relative)
                self.assertIn("| length > 0\n", text)
                self.assertIn(".json.response.uuid | default('')", text)

    def test_a_real_run_still_requires_every_uuid(self) -> None:
        # The empty fallback above must never make a real run pass silently.
        for relative, name in [
            ("roles/remnawave_panel/tasks/node.yml", "Require a Node UUID on a real run"),
            (
                "roles/remnawave_panel/tasks/squad.yml",
                "Require an Internal Squad UUID on a real run",
            ),
            (
                "roles/remnawave_panel/tasks/hosts.yml",
                "Require one reconciled UUID for every declared Host",
            ),
        ]:
            with self.subTest(task=name):
                self.assertEqual(self.task(relative, name).get("when"), "not ansible_check_mode")

    def test_a_dry_run_stops_instead_of_configuring_an_unregistered_node(self) -> None:
        main = self.read("roles/remnawave_panel/tasks/main.yml")
        self.assertIn("remnawave_panel_pending_objects", main)
        self.assertIn("ansible.builtin.meta: end_host", main)
        profile = self.read("roles/remnawave_panel/tasks/profile.yml")
        self.assertIn("End the dry-run for a node that has no Reality key yet", profile)

    def test_certificate_checks_wait_for_a_certificate_to_exist(self) -> None:
        issue = self.read("roles/remnawave_node/tasks/certificate_issue.yml")
        plan = self.read("roles/remnawave_node/tasks/certificate.yml")
        self.assertIn(
            "when: remnawave_certificate_before_phase.stat.exists or not ansible_check_mode", issue
        )
        self.assertIn(
            "when: remnawave_certificate_file.stat.exists or not ansible_check_mode", plan
        )
        # ...and the assert is still there for a real run.
        self.assertIn("when: not ansible_check_mode", issue)
        self.assertIn("when: remnawave_certificate_final is not skipped", plan)

    def test_ssh_policy_is_still_proven_when_the_dropin_is_unchanged(self) -> None:
        # The check must only stand down where this run would have rewritten the
        # drop-in; on every re-run it has to execute for real.
        system = self.read("roles/node_base/tasks/system.yml")
        self.assertIn("register: node_base_ssh_dropin", system)
        self.assertIn(
            "when: not (ansible_check_mode and node_base_ssh_dropin is changed)", system
        )

    def test_acceptance_stands_down_only_for_what_the_run_would_change(self) -> None:
        local = self.read("roles/node_verify/tasks/local.yml")
        self.assertIn(
            "not (ansible_check_mode and node_base_firewall_template.changed | default(false))",
            local,
        )
        # Containers, listeners, logs and the Panel's view stay asserted.
        self.assertIn("fail_msg: RemnaNode or nginx selfsteal is absent, stopped or unhealthy.", local)
        self.assertNotIn("ignore_errors", local)

    def test_the_tunnel_probe_is_announced_rather_than_faked(self) -> None:
        probe = self.read("roles/node_verify/tasks/tunnel_probe.yml")
        self.assertIn("when: not ansible_check_mode", probe)
        self.assertIn("Report that a dry-run cannot carry real traffic through the node", probe)
        # The panel-side preconditions are still checked for real.
        self.assertIn("check_mode: false", probe)

    def test_no_check_is_disabled_by_brute_force(self) -> None:
        # The two shortcuts that would make every dry-run "pass": swallowing the
        # result of a task, and swallowing it *and* skipping it in check mode.
        for path in sorted((ROOT / "roles").rglob("*.yml")):
            text = path.read_text(encoding="utf-8")
            with self.subTest(file=str(path.relative_to(ROOT))):
                self.assertNotIn("ignore_errors: true", text)
            document = yaml.safe_load(text)
            if not isinstance(document, list):
                continue
            for task in document:
                if not isinstance(task, dict):
                    continue
                condition = str(task.get("when", "")).strip()
                if task.get("failed_when") is False and condition == "not ansible_check_mode":
                    self.fail(
                        f"{path.relative_to(ROOT)}: '{task.get('name')}' both hides its result and "
                        "skips itself in check mode, which is how a check that proves nothing looks"
                    )
