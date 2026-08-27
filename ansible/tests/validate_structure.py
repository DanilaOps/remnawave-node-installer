from __future__ import annotations

import pathlib
import re
import sys


ROOT = pathlib.Path(__file__).parents[1]
ROLES = ROOT / "roles"
# node_bootstrap is the only role that runs before the managed account exists:
# it connects as root, may have no Python on the target yet and never escalates.
# Those connection semantics cannot live inside node_base.
EXPECTED_ROLES = {
    "node_bootstrap",
    "dns",
    "node_base",
    "remnawave_panel",
    "remnawave_node",
    "node_verify",
}


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


actual_roles = {path.name for path in ROLES.iterdir() if path.is_dir()}
if actual_roles != EXPECTED_ROLES:
    fail(f"expected exactly {sorted(EXPECTED_ROLES)}, got {sorted(actual_roles)}")

for role in EXPECTED_ROLES:
    role_dir = ROLES / role
    for required in ("defaults/main.yml", "meta/argument_specs.yml", "tasks/main.yml"):
        if not (role_dir / required).is_file():
            fail(f"{role}/{required} is missing")

task_reference = re.compile(
    r"ansible\.builtin\.(?:import_tasks|include_tasks):\s*([^\s#]+)"
)
template_reference = re.compile(r"src:\s*([A-Za-z0-9_.-]+\.(?:j2|yml))\s*$")

for task_file in ROLES.glob("*/tasks/*.yml"):
    text = task_file.read_text(encoding="utf-8")
    for reference in task_reference.findall(text):
        reference = reference.strip("'\"")
        if "{{" not in reference and not (task_file.parent / reference).is_file():
            fail(f"{task_file.relative_to(ROOT)} references missing task {reference}")
    for reference in template_reference.findall(text):
        template = task_file.parents[1] / "templates" / reference
        if not template.is_file():
            fail(f"{task_file.relative_to(ROOT)} references missing template {reference}")

PROJECT_YAML_ROOTS = (
    ROOT / "inventories",
    ROOT / "playbooks",
    ROOT / "roles",
    ROOT / "tests",
)
for forbidden in ("ignore_errors: true", "curl | bash", "curl | sh"):
    for search_root in PROJECT_YAML_ROOTS:
        for path in search_root.rglob("*"):
            if path.is_file() and path.suffix in {".yml", ".yaml", ".j2"}:
                if forbidden in path.read_text(encoding="utf-8", errors="ignore"):
                    fail(
                        f"forbidden pattern {forbidden!r} "
                        f"in {path.relative_to(ROOT)}"
                    )

print("Ansible structure and static references are consistent.")
