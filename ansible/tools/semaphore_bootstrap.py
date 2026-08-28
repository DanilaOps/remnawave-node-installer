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

# Semaphore clones the repository fresh for every job, so nothing that is not
# committed reaches a run started from the UI - and this repository is public, so
# the real values of the deployment cannot be committed. Both files therefore live
# on the controller and are loaded as extra-vars files named in every template's
# arguments. ./provision-node loads the same two files, so the UI and the command
# line read one source. An extra-vars file is also how a secret stays off the
# command line: Ansible decrypts it in-process, unlike -e name=value.
DEFAULT_FLEET_VALUES = "/etc/remnawave/fleet.yml"
DEFAULT_SECRET_VALUES = "/etc/remnawave/secrets.yml"
DEFAULT_VAULT_PASSWORD = "/etc/remnawave/vault-pass"

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


def add_project_user(api: "Semaphore", project_id: int, arguments) -> None:
    """Create a second operator account and give it access to this project.

    Semaphore 2.18.29 has no self-registration route at all - accounts exist only
    because an admin created them - so this is the only way a teammate gets in,
    and it is deliberately not an admin: task_runner may press the buttons and
    read the logs, but cannot rewrite a template into something else.
    """

    login = arguments.add_user
    password = os.environ.get("SEMAPHORE_NEW_USER_PASSWORD", "")

    users = api.get("/users")
    user = next(
        (item for item in users or [] if item.get("username") == login),
        None,
    )
    if user is None:
        if not password:
            raise ApiError(
                f"user '{login}' does not exist and SEMAPHORE_NEW_USER_PASSWORD is not set. "
                "Export it for this one command; it is never written to the repository."
            )
        print(f"  user '{login}': creating (not an admin)")
        user = api.post(
            "/users",
            {
                "name": arguments.user_name or login,
                "username": login,
                "email": arguments.user_email or f"{login}@localhost",
                "password": password,
                "admin": False,
            },
        )
    else:
        print(f"  user '{login}': exists (id {user['id']}), password left alone")

    members = api.get(f"/project/{project_id}/users")
    member = next(
        (item for item in members or [] if item.get("id") == user.get("id")),
        None,
    )
    if member is None:
        print(f"  project access for '{login}': granting {arguments.user_role}")
        api.post(
            f"/project/{project_id}/users",
            {"project_id": project_id, "user_id": user["id"], "role": arguments.user_role},
        )
    else:
        print(
            f"  project access for '{login}': already {member.get('role', 'unknown')}, left alone"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="print, change nothing")
    parser.add_argument("--update", action="store_true", help="correct managed fields of existing objects")
    parser.add_argument("--repository-url", default=os.environ.get("SEMAPHORE_REPOSITORY_URL", ""))
    parser.add_argument("--branch", default=os.environ.get("SEMAPHORE_REPOSITORY_BRANCH", "main"))
    parser.add_argument("--ssh-key-name", default="deployer", help="Key Store entry used to reach nodes")
    parser.add_argument(
        "--fleet-values",
        default=os.environ.get("REMNAWAVE_FLEET_VARS", DEFAULT_FLEET_VALUES),
        help="deployment values file on the controller, loaded by every template",
    )
    parser.add_argument(
        "--secret-values",
        default=os.environ.get("REMNAWAVE_SECRET_VARS", DEFAULT_SECRET_VALUES),
        help="ansible-vault encrypted secrets file on the controller",
    )
    parser.add_argument(
        "--add-user",
        default="",
        metavar="LOGIN",
        help="create this login if absent and give it access to the project",
    )
    parser.add_argument("--user-name", default="", help="display name for --add-user")
    parser.add_argument("--user-email", default="", help="email for --add-user")
    parser.add_argument(
        "--user-role",
        default="task_runner",
        choices=["owner", "manager", "task_runner", "guest"],
        help=(
            "project role for --add-user. task_runner may run the templates but not "
            "change them, which is what a second operator needs"
        ),
    )
    parser.add_argument(
        "--vault-password-file",
        default=os.environ.get("ANSIBLE_VAULT_PASSWORD_FILE", DEFAULT_VAULT_PASSWORD),
        help="vault password file on the controller, exported to every run",
    )
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
                {
                    "name": ENVIRONMENT,
                    "project_id": project_id,
                    # No values here on purpose. Variable-group values arrive as
                    # extra vars, which outrank every file and every host: a
                    # node_id or an ansible_host placed here would be applied to
                    # the whole fleet at once, and every run would overwrite the
                    # previous node's panel objects. Preflight refuses a run whose
                    # identity did not come from the inventory hostname, so that
                    # mistake now fails instead of being published.
                    "json": "{}",
                    # Only where to find the vault password - not a secret itself.
                    "env": json.dumps(
                        {"ANSIBLE_VAULT_PASSWORD_FILE": arguments.vault_password_file}
                    ),
                },
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
                "arguments": json.dumps(
                    ["-e", f"@{arguments.fleet_values}", "-e", f"@{arguments.secret_values}"]
                    + wanted["arguments"]
                ),
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

        if arguments.add_user:
            add_project_user(api, project_id, arguments)

        print(
            f"\nEvery template loads {arguments.fleet_values} and {arguments.secret_values}\n"
            f"and reads the vault password from {arguments.vault_password_file}. Those three files\n"
            "live on the controller, not in the repository and not in Semaphore's database.\n"
        )
        print(
            "\nStill to do by hand, because it needs secrets or judgement:\n"
            "  - Key Store: the deployer SSH private key and the Ansible vault password\n"
            "  - /etc/remnawave/fleet.yml and secrets.yml on the controller, from\n"
            "    ansible/examples/*.example - without them every run stops in preflight\n"
            "  - Inventory: one line per node"
        )
        print(
            "\nOn a published instance, check once by hand:\n"
            "  - there is no self-registration route in 2.18.29, so no setting to turn off:\n"
            "    accounts exist only because an admin created them. Verify the user list holds\n"
            "    only the people you expect.\n"
            "  - /api/auth/recovery, /api/integrations/*, /api/terraform/* and\n"
            "    /api/internal/* are served without authentication. The reverse proxy blocks\n"
            "    the last three; recovery stays open because it is the login page's own flow."
        )
    except ApiError as error:
        print(f"\nSemaphore API refused the request:\n  {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
