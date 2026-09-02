#!/usr/bin/env python3
"""The node_monitoring removal path must know everything the install path adds.

Ansible manages the node-side agent and nothing else: the central monitoring
server is installed by hand. So the only lifecycle this repository has to keep
honest is this one, and the failure it prevents is quiet - a rollback that
misses one unit leaves a service running against files that are gone, and the
next install finds a node that is neither clean nor converged. Nothing in
Ansible notices; tasks/remove.yml simply never mentions the thing.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
NODE = REPO / "roles/node_monitoring"
NODE_BASE = REPO / "roles/node_base"

UNIT_DEST = re.compile(r"dest:\s*/etc/systemd/system/(\S+)")


def defaults() -> dict:
    return yaml.safe_load((NODE / "defaults/main.yml").read_text(encoding="utf-8"))


def units_written() -> set[str]:
    found: set[str] = set()
    for path in (NODE / "tasks").glob("*.yml"):
        if path.name == "remove.yml":
            continue
        for match in UNIT_DEST.finditer(path.read_text(encoding="utf-8")):
            found.add(match.group(1))
    return found


def task_names(path: Path) -> list[str]:
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    names: list[str] = []

    def walk(tasks):
        for task in tasks:
            if not isinstance(task, dict):
                continue
            if "name" in task:
                names.append(task["name"])
            for key in ("block", "rescue", "always"):
                if key in task:
                    walk(task[key])

    walk(document)
    return names


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.defaults = defaults()

    def test_state_defaults_to_present(self):
        self.assertEqual(self.defaults["node_monitoring_state"], "present")

    def test_the_agent_is_off_by_default(self):
        # Monitoring is additive. A fleet that has not asked for it gets
        # nothing, which is why the role can live in the ordinary node install.
        # The switch lives in node_base, with the firewall rule it controls.
        base = yaml.safe_load((NODE_BASE / "defaults/main.yml").read_text(encoding="utf-8"))
        self.assertFalse(base["node_monitoring_enabled"])
        self.assertEqual(base["monitoring_scrape_cidrs"], [])

    def test_every_unit_the_role_writes_is_in_the_removal_list(self):
        written = units_written()
        listed = set(self.defaults["node_monitoring_units"])
        self.assertTrue(written, "no unit files found in the install tasks")
        self.assertEqual(
            written - listed,
            set(),
            "the install path writes units the removal path never removes",
        )

    def test_the_removal_list_has_no_unit_the_role_never_writes(self):
        self.assertEqual(set(self.defaults["node_monitoring_units"]) - units_written(), set())

    def test_main_refuses_an_unknown_state(self):
        text = (NODE / "tasks/main.yml").read_text(encoding="utf-8")
        self.assertIn("node_monitoring_state in ['present', 'absent']", text)
        self.assertIn("node_monitoring_state == 'absent'", text)
        self.assertIn("node_monitoring_state == 'present'", text)

    def test_removal_stops_services_before_deleting_their_units(self):
        names = task_names(NODE / "tasks/remove.yml")
        self.assertLess(
            names.index("Stop and disable the node monitoring units"),
            names.index("Remove the node monitoring unit files"),
        )

    def test_removal_reloads_systemd_after_deleting_units(self):
        names = task_names(NODE / "tasks/remove.yml")
        self.assertLess(
            names.index("Remove the node monitoring unit files"),
            names.index("Reload systemd after removing the units"),
        )

    def test_removal_does_not_touch_the_firewall(self):
        # The rule that restricts the exporter port belongs to node_base. This
        # role reaching into another role's chain is how a rollback of an
        # optional extra takes a node's SSH with it.
        text = (NODE / "tasks/remove.yml").read_text(encoding="utf-8")
        self.assertNotIn("nft", text.replace("was not touched", ""))
        self.assertIn("node_base", text)

    def test_the_installer_runs_the_role_when_it_is_enabled_or_being_removed(self):
        # A rollback has to reach a node whose node_monitoring_enabled is
        # already false, or turning the flag off would strand the agent.
        text = (REPO / "playbooks/install_node.yml").read_text(encoding="utf-8")
        self.assertIn("node_monitoring", text)
        self.assertIn("node_monitoring_state | default('present') == 'absent'", text)


class NoCentralStackTests(unittest.TestCase):
    """Ansible installs the node agent. It does not install the server.

    This is an architectural boundary, not a preference, and the way it erodes
    is one task at a time. These checks fail the moment a role starts fetching
    Prometheus again.
    """

    FORBIDDEN_ROLES = ("monitoring_stack",)
    SERVER_SOFTWARE = ("prometheus", "grafana", "alertmanager", "blackbox_exporter")

    def test_the_central_stack_role_is_gone(self):
        for name in self.FORBIDDEN_ROLES:
            self.assertFalse(
                (REPO / "roles" / name).exists(),
                f"role {name} is back; the monitoring server is installed by hand",
            )

    def test_no_playbook_references_the_central_stack(self):
        for path in (REPO / "playbooks").rglob("*.yml"):
            text = path.read_text(encoding="utf-8")
            for name in self.FORBIDDEN_ROLES:
                self.assertNotIn(name, text, f"{path.name} references {name}")

    def test_no_role_downloads_monitoring_server_software(self):
        for path in (REPO / "roles").rglob("*.yml"):
            text = path.read_text(encoding="utf-8").lower()
            self.assertNotIn("dl.grafana.com", text, str(path))
            for name in self.SERVER_SOFTWARE:
                self.assertNotIn(
                    f"releases/download/v{name}", text, f"{path} downloads {name}"
                )

    def test_the_only_monitoring_role_is_the_node_agent(self):
        roles = {p.name for p in (REPO / "roles").iterdir() if p.is_dir()}
        monitoring_roles = {name for name in roles if "monitor" in name}
        self.assertEqual(monitoring_roles, {"node_monitoring"})

    def test_node_exporter_is_the_only_release_the_agent_fetches(self):
        # The URLs are assembled from variables, so check the parts: the host
        # it downloads from, and the repository it downloads out of. A second
        # host appearing here means the node role has started installing
        # something that belongs on the monitoring server.
        values = yaml.safe_load((NODE / "defaults/main.yml").read_text(encoding="utf-8"))
        text = (NODE / "defaults/main.yml").read_text(encoding="utf-8")
        hosts = set(re.findall(r"https://([a-z0-9.-]+)", text))
        self.assertEqual(hosts, {"github.com"}, f"unexpected download host: {hosts}")
        self.assertEqual(
            values["node_exporter_release_repository"],
            "prometheus/node_exporter",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
