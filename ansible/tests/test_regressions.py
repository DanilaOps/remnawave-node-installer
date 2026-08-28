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
        self.assertTrue((ROOT / "examples" / "vault.yml.example").is_file())
        self.assertTrue((ROOT / "examples" / "local-overrides.yml.example").is_file())
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

    def test_local_overrides_win_over_the_published_documentation_values(self) -> None:
        # The real deployment values are loaded last of the group files, so they
        # override the documentation values that are safe to publish. Both are in
        # the same precedence tier, so only the file name decides.
        tracked = sorted(
            path.name for path in (GROUP_VARS / "remnawave_nodes").glob("*.yml")
        )
        self.assertTrue(tracked)
        for name in tracked:
            self.assertLess(
                name, "zz-local.yml",
                "zz-local.yml must sort after every tracked group file",
            )
        gitignore = (REPO / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(
            "ansible/playbooks/group_vars/remnawave_nodes/zz-local.yml", gitignore
        )
        self.assertIn("ansible/playbooks/group_vars/all/vault.yml", gitignore)

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

    def test_the_controller_does_not_touch_ssh_authentication(self) -> None:
        # The controller is the machine an operator must be able to get back
        # into; this role is never the reason they cannot.
        for path in (ROOT / "roles" / "semaphore_controller").rglob("*"):
            if path.is_file():
                text = path.read_text(encoding="utf-8")
                for forbidden in ("PermitRootLogin", "PasswordAuthentication", "sshd_config"):
                    self.assertNotIn(forbidden, text, f"{path} changes SSH policy")

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


if __name__ == "__main__":
    unittest.main()
