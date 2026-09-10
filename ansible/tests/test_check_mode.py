#!/usr/bin/env python3
"""Guard the dry-run: every register a check-mode run cannot fill must have a guarded consumer.

Ansible skips command, shell, raw, script, uri, wait_for and wait_for_connection in
check mode no matter what their `when` says. Anything registered by such a task is
therefore an empty result in a dry-run, and a consumer that dereferences it - an
`assert`, a `set_fact`, a `loop` - fails with an undefined attribute. That failure
is not a finding about the servers; it is a bug in the playbook, and it used to be
found only by running --check against a real host.

A task escapes the rule in one of three ways, each of which is a deliberate
decision the author has to write down:

  check_mode: false      the task only reads, so it may run in a dry-run too and
                         the dry-run then sees real state (preferred);
  when: ... ansible_check_mode
                         the task, or the block around it, is explicitly not part
                         of a dry-run, and something else reports that;
  consumer guarded       the consumer tests `<var> is skipped` / `is not skipped`,
                         or reads the value through `default(...)`.

Run: python ansible/tests/test_check_mode.py
"""
from __future__ import annotations

import pathlib
import re
import sys
import unittest

import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
ANSIBLE = REPO / "ansible"
IGNORED_PARTS = {".venv", ".venv-audit", "collections", "molecule", "__pycache__"}

# Action plugins Ansible refuses to execute in check mode.
SKIPPING_MODULES = {
    "command", "shell", "raw", "script", "uri", "wait_for", "wait_for_connection",
    "ansible.builtin.command", "ansible.builtin.shell", "ansible.builtin.raw",
    "ansible.builtin.script", "ansible.builtin.uri", "ansible.builtin.wait_for",
    "ansible.builtin.wait_for_connection",
}
BLOCK_KEYS = ("block", "rescue", "always")
GUARD_PATTERNS = ("is skipped", "is not skipped", "default(")


def task_files() -> list[pathlib.Path]:
    files = []
    for path in sorted(ANSIBLE.rglob("*.yml")):
        if any(part in IGNORED_PARTS for part in path.parts):
            continue
        parent = path.parent.name
        if parent in {"tasks", "handlers", "playbooks", "tests"} or path.parent.parent.name == "playbooks":
            files.append(path)
    return files


def flatten(entries, inherited_when, inherited_check_mode):
    """Yield (task, when_chain, check_mode) for every task, descending into blocks."""
    if not isinstance(entries, list):
        return
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        when = list(inherited_when)
        if "when" in entry:
            value = entry["when"]
            when.extend(value if isinstance(value, list) else [value])
        check_mode = entry.get("check_mode", inherited_check_mode)
        if any(key in entry for key in BLOCK_KEYS):
            for key in BLOCK_KEYS:
                yield from flatten(entry.get(key), when, check_mode)
            continue
        yield entry, when, check_mode


def tasks_of(path: pathlib.Path):
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if isinstance(document, list) and document and isinstance(document[0], dict) and (
        "hosts" in document[0]
    ):
        # A playbook: audit its own tasks sections, roles are audited on their own.
        for play in document:
            if not isinstance(play, dict):
                continue
            for section in ("pre_tasks", "tasks", "post_tasks", "handlers"):
                yield from flatten(play.get(section), [], None)
        return
    yield from flatten(document, [], None)


def module_of(task: dict) -> str | None:
    for key in task:
        if key in SKIPPING_MODULES:
            return key
    return None


def when_text(when_chain) -> str:
    return " ".join(str(item) for item in when_chain)


def body_text(task: dict) -> str:
    return yaml.safe_dump(task, default_flow_style=False, sort_keys=True)


def findings() -> list[str]:
    problems = []
    for path in task_files():
        parsed = list(tasks_of(path))
        for task, when_chain, check_mode in parsed:
            module = module_of(task)
            register = task.get("register")
            if not module or not register:
                continue
            if check_mode is False:
                continue
            if "ansible_check_mode" in when_text(when_chain):
                continue
            for other, other_when, _ in parsed:
                if other is task:
                    continue
                text = body_text(other) + " " + when_text(other_when)
                if not re.search(rf"\b{re.escape(register)}\b", text):
                    continue
                if "ansible_check_mode" in when_text(other_when):
                    continue
                if any(pattern in text for pattern in GUARD_PATTERNS):
                    continue
                problems.append(
                    f"{path.relative_to(REPO)}: '{task.get('name', module)}' registers "
                    f"'{register}' with {module}, which check mode skips, and "
                    f"'{other.get('name', '<unnamed>')}' reads it without a guard"
                )
    return problems


class CheckModeRegisterTests(unittest.TestCase):
    def test_no_unguarded_register_from_a_check_mode_skipping_module(self):
        problems = findings()
        self.assertEqual([], problems, "\n" + "\n".join(problems))

    def test_the_audit_actually_inspects_the_roles(self):
        # A silent zero because nothing was parsed would make the test above useless.
        files = task_files()
        self.assertGreater(len(files), 30, "the audit found almost no task files")
        registers = [
            task
            for path in files
            for task, _when, _check in tasks_of(path)
            if module_of(task) and task.get("register")
        ]
        self.assertGreater(len(registers), 20, "the audit found almost no registers to inspect")


# Modules that ask systemd about a named unit. In check mode they still query
# systemd, so a unit that only *would* have been installed makes them fail with
# "Could not find the requested service".
UNIT_MODULES = {
    "ansible.builtin.systemd_service", "ansible.builtin.systemd", "ansible.builtin.service",
    "systemd_service", "systemd", "service",
}
# Modules that act on something a planned write would have produced.
PLANNED_WRITE_CONSUMERS = UNIT_MODULES | {"community.docker.docker_compose_v2"}
# A task is allowed past the rule if its condition consults one of these: either
# it excludes check mode outright, or it asks whether systemd really knows the
# unit right now.
UNIT_EXISTENCE_SIGNALS = (
    "ansible_check_mode",
    "ansible_facts.services",
    "controller_systemd_units",
    "node_base_systemd_units",
)


def unit_module_of(task: dict) -> str | None:
    for key in task:
        if key in PLANNED_WRITE_CONSUMERS:
            return key
    return None


class PlannedWriteConsumerTests(unittest.TestCase):
    """A dry-run installs no unit file and no package.

    Ansible reports `changed` for a template or an apt install it only simulated,
    but nothing appears on disk. A systemd task that then starts, enables or
    restarts that unit is asking systemd about something that does not exist, and
    the run stops on "Could not find the requested service" - which is what
    happened to `Arm the rollback timer` after the first check-mode audit, because
    that audit only looked at `register` chains.

    The rule: a task that acts on a named unit must say, in its own condition or
    in the condition of a block around it, how it knows the unit is there. Either
    it is not part of a dry-run at all, or it consults what systemd currently
    knows. `daemon_reload` on its own names no unit and is exempt.
    """

    def offenders(self) -> list[str]:
        problems = []
        for path in task_files():
            for task, when_chain, _check_mode in tasks_of(path):
                module = unit_module_of(task)
                if not module:
                    continue
                args = task.get(module) or {}
                if not isinstance(args, dict):
                    continue
                if module in UNIT_MODULES:
                    # daemon_reload with no unit name touches no unit file.
                    if not args.get("name"):
                        continue
                    if not any(key in args for key in ("state", "enabled", "masked")):
                        continue
                condition = when_text(when_chain)
                if any(signal in condition for signal in UNIT_EXISTENCE_SIGNALS):
                    continue
                problems.append(
                    f"{path.relative_to(REPO)}: '{task.get('name', module)}' acts on "
                    f"{args.get('name', module)} without saying how a dry-run knows it exists"
                )
        return problems

    def test_no_systemd_task_assumes_a_unit_a_dry_run_did_not_install(self):
        problems = self.offenders()
        self.assertEqual([], problems, "\n" + "\n".join(problems))

    def test_the_rule_actually_has_tasks_to_check(self):
        checked = [
            task
            for path in task_files()
            for task, _when, _check in tasks_of(path)
            if unit_module_of(task)
        ]
        self.assertGreater(len(checked), 15, "the systemd audit found almost nothing to inspect")

    def test_the_firewall_rollback_chain_never_touches_systemd_in_a_dry_run(self):
        # The exact chain that failed on the controller, in both roles that have it.
        for relative, timer in [
            ("roles/semaphore_controller/tasks/firewall.yml",
             "remnawave-controller-firewall-rollback.timer"),
            ("roles/node_base/tasks/firewall.yml", "remnawave-firewall-rollback.timer"),
        ]:
            text = (ANSIBLE / relative).read_text(encoding="utf-8")
            with self.subTest(file=relative):
                self.assertIn(timer, text)
                # Arming and disarming are changes to a live machine: never in a dry-run.
                for fragment in text.split("- name: ")[1:]:
                    if timer in fragment and "systemd_service" in fragment:
                        self.assertIn("not ansible_check_mode", fragment)
                # ...and a dry-run has to say what it would have done instead.
                self.assertIn("Say what a dry-run would do to the live firewall", text)

    def test_every_service_handler_reports_itself_in_a_dry_run(self):
        # A guarded handler that says nothing would make a dry-run quieter than
        # the truth: the operator still has to see which services a real run
        # restarts.
        for relative in [
            "roles/semaphore_controller/handlers/main.yml",
            "roles/node_base/handlers/main.yml",
            "roles/remnawave_node/handlers/main.yml",
        ]:
            document = yaml.safe_load((ANSIBLE / relative).read_text(encoding="utf-8"))
            guarded = [
                handler["name"]
                for handler in document
                if "not ansible_check_mode" in str(handler.get("when", ""))
            ]
            listeners = {str(handler.get("listen", "")) for handler in document}
            for name in guarded:
                with self.subTest(handler=f"{relative}:{name}"):
                    self.assertIn(name, listeners, f"{name} is silent in a dry-run")


if __name__ == "__main__":
    problems = findings()
    for problem in problems:
        print(problem)
    print(f"\n{len(problems)} unguarded register(s)")
    sys.exit(1 if problems else 0)
