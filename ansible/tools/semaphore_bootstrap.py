#!/usr/bin/env python3
"""Create the Semaphore project, inventory and the three templates, repeatably.

Why this exists: clicking a project together once is fine until the controller
has to be rebuilt, and then nobody remembers which flags each template carried.
This declares them.

It is idempotent by name: an object that already exists is left alone unless
--update is given, in which case its managed fields are corrected. It never
writes a secret into the repository - every credential comes from a file or the
environment - and it never guesses: an unexpected API answer is printed with its
status and body, and the tool stops.

    export SEMAPHORE_URL=http://127.0.0.1:3000
    export SEMAPHORE_API_TOKEN="$(cat /etc/semaphore/bootstrap-api-token)"
    python3 ansible/tools/semaphore_bootstrap.py --dry-run
    python3 ansible/tools/semaphore_bootstrap.py

Run it with --dry-run first: it prints exactly what it would create.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

PROJECT = "Remnawave Nodes"
REPOSITORY = "remnawave-node-installer"
INVENTORY = "Production nodes"
ENVIRONMENT = "Production"
PLAYBOOK = "ansible/playbooks/provision_node.yml"

TEMPLATES = [
    {
        "name": "01 - Preflight",
        "description": (
            "Read-only. Checks the inventory, the panel, the shared Config Profile and "
            "conflicts, and reports the DNS state without changing it."
        ),
        "arguments": ["--tags", "preflight", "--check"],
        "allow_parallel_tasks": True,
        "survey_vars": [],
    },
    {
        "name": "02 - Install / Reconcile Node",
        "description": (
            "The only mutating template. Installs a new node and brings an existing one "
            "back to the declared state - this is also how a node is updated and repaired. "
            "Put the node name in Limit."
        ),
        "arguments": [],
        "allow_parallel_tasks": False,
        "survey_vars": [
            {
                "name": "bootstrap_ssh_password",
                "title": "Fresh VPS root password",
                "type": "secret",
                "description": "Leave empty after the deployer account has been created.",
            },
            {
                "name": "bootstrap_trust_new_host_keys",
                "title": "Trust a new SSH host key",
                "required": True,
                "type": "enum",
                "description": "Enable only for an explicitly accepted fresh VPS.",
                "values": [
                    {"name": "No", "value": "false"},
                    {"name": "Yes - fresh VPS", "value": "true"},
                ],
                "default_value": "false",
            },
        ],
    },
    {
        "name": "03 - Verify Node",
        "description": (
            "Read-only. Re-checks the running node, the panel links and the end-to-end "
            "tunnel without changing anything."
        ),
        "arguments": ["--tags", "node_verify"],
        "allow_parallel_tasks": True,
        "survey_vars": [],
    },
]


class ApiError(RuntimeError):
    pass


class Semaphore:
    def __init__(self, base: str, token: str, dry_run: bool) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self.dry_run = dry_run

    def request(self, method: str, path: str, payload: dict | None = None):
        url = f"{self.base}/api{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self.token}")
        if data:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode() or "null"
                return json.loads(body)
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")[:800]
            raise ApiError(f"{method} {path} -> HTTP {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            raise ApiError(
                f"{method} {path} -> cannot reach {self.base}: {error.reason}. "
                "Open the SSH tunnel first."
            ) from error

    def get(self, path: str):
        return self.request("GET", path)

    def post(self, path: str, payload: dict):
        if self.dry_run:
            print(f"    would POST {path} {json.dumps(payload, sort_keys=True)}")
            return {"id": 0, "dry_run": True}
        return self.request("POST", path, payload)

    def put(self, path: str, payload: dict):
        if self.dry_run:
            print(f"    would PUT {path} {json.dumps(payload, sort_keys=True)}")
            return None
        return self.request("PUT", path, payload)


def find(items, name: str):
    for item in items or []:
        if isinstance(item, dict) and item.get("name") == name:
            return item
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="print, change nothing")
    parser.add_argument("--update", action="store_true", help="correct managed fields of existing objects")
    parser.add_argument("--repository-url", default=os.environ.get("SEMAPHORE_REPOSITORY_URL", ""))
    parser.add_argument("--branch", default=os.environ.get("SEMAPHORE_REPOSITORY_BRANCH", "main"))
    parser.add_argument("--ssh-key-name", default="deployer", help="Key Store entry used to reach nodes")
    arguments = parser.parse_args()

    base = os.environ.get("SEMAPHORE_URL", "http://127.0.0.1:3000")
    token = os.environ.get("SEMAPHORE_API_TOKEN", "")
    if not token:
        print(
            "SEMAPHORE_API_TOKEN is not set. Create an API token in the UI "
            "(User settings -> API tokens) and export it; it is never stored here.",
            file=sys.stderr,
        )
        return 2

    api = Semaphore(base, token, arguments.dry_run)
    try:
        print(f"Semaphore at {base}")
        projects = api.get("/projects")
        project = find(projects, PROJECT)
        if project is None:
            print(f"  project '{PROJECT}': creating")
            project = api.post("/projects", {"name": PROJECT, "alert": False})
        else:
            print(f"  project '{PROJECT}': exists (id {project['id']})")
        project_id = project["id"]
        scope = f"/project/{project_id}"

        keys = api.get(f"{scope}/keys")
        key = find(keys, arguments.ssh_key_name)
        if key is None:
            print(
                f"  key '{arguments.ssh_key_name}': MISSING. Add it in the UI as an SSH key "
                "(Key Store) - a private key is never written by this tool.",
                file=sys.stderr,
            )
            return 3
        print(f"  key '{arguments.ssh_key_name}': exists (id {key['id']})")

        repositories = api.get(f"{scope}/repositories")
        repository = find(repositories, REPOSITORY)
        if repository is None:
            if not arguments.repository_url:
                print(
                    "  repository: MISSING and --repository-url was not given.",
                    file=sys.stderr,
                )
                return 4
            print(f"  repository '{REPOSITORY}': creating ({arguments.branch})")
            repository = api.post(
                f"{scope}/repositories",
                {
                    "name": REPOSITORY,
                    "project_id": project_id,
                    "git_url": arguments.repository_url,
                    "git_branch": arguments.branch,
                    "ssh_key_id": key["id"],
                },
            )
        else:
            print(f"  repository '{REPOSITORY}': exists (id {repository['id']})")

        inventories = api.get(f"{scope}/inventory")
        inventory = find(inventories, INVENTORY)
        if inventory is None:
            print(f"  inventory '{INVENTORY}': creating (empty static YAML - add nodes in the UI)")
            inventory = api.post(
                f"{scope}/inventory",
                {
                    "name": INVENTORY,
                    "project_id": project_id,
                    "type": "static-yaml",
                    "ssh_key_id": key["id"],
                    "inventory": (
                        "---\n"
                        "# One line per node. Identity comes from the hostname.\n"
                        "all:\n  children:\n    remnawave_nodes:\n      hosts: {}\n"
                    ),
                },
            )
        else:
            print(f"  inventory '{INVENTORY}': exists (id {inventory['id']})")

        environments = api.get(f"{scope}/environment")
        environment = find(environments, ENVIRONMENT)
        if environment is None:
            print(f"  variable group '{ENVIRONMENT}': creating (secrets are added in the UI)")
            environment = api.post(
                f"{scope}/environment",
                {"name": ENVIRONMENT, "project_id": project_id, "json": "{}", "env": "{}"},
            )
        else:
            print(f"  variable group '{ENVIRONMENT}': exists (id {environment['id']})")

        existing_templates = api.get(f"{scope}/templates")
        for wanted in TEMPLATES:
            template = find(existing_templates, wanted["name"])
            payload = {
                "name": wanted["name"],
                "description": wanted["description"],
                "project_id": project_id,
                "inventory_id": inventory["id"],
                "repository_id": repository["id"],
                "environment_id": environment["id"],
                "playbook": PLAYBOOK,
                "arguments": json.dumps(wanted["arguments"]),
                "survey_vars": wanted["survey_vars"],
                "allow_parallel_tasks": wanted["allow_parallel_tasks"],
                "allow_override_args_in_task": False,
                "app": "ansible",
            }
            if template is None:
                print(f"  template '{wanted['name']}': creating")
                api.post(f"{scope}/templates", payload)
            elif arguments.update:
                print(f"  template '{wanted['name']}': updating managed fields")
                api.put(f"{scope}/templates/{template['id']}", {**payload, "id": template["id"]})
            else:
                print(f"  template '{wanted['name']}': exists (id {template['id']}, left alone)")

        print(
            "\nStill to do by hand, because it needs secrets or judgement:\n"
            "  - Key Store: the deployer SSH private key and the Ansible vault password\n"
            "  - Variable group: the panel token as a secret\n"
            "  - Inventory: one line per node"
        )
    except ApiError as error:
        print(f"\nSemaphore API refused the request:\n  {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
