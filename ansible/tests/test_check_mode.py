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


if __name__ == "__main__":
    problems = findings()
    for problem in problems:
        print(problem)
    print(f"\n{len(problems)} unguarded register(s)")
    sys.exit(1 if problems else 0)
