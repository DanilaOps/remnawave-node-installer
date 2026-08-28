# Semaphore controller

Semaphore clones the repository and runs playbooks from its **root**, which is
where the single `ansible.cfg` lives — the same way the wrappers and the test
suite run them. Nothing needs `ANSIBLE_CONFIG` or any other environment
variable.

Pinned version: **Semaphore 2.18.29**. Recorded here because the controller has
to be rebuildable, and because two behaviours of this exact version are relied
on (see below).

## Files here

- `semaphore.service` — systemd unit. Binds the UI to `127.0.0.1:3000` only, runs
  as its own system user, keeps a persistent `HOME` so the SSH `known_hosts`
  written on a node's first run survives the next one, and restricts the
  filesystem (`ProtectHome`, `ProtectSystem=strict`, narrow `ReadWritePaths`,
  `NoNewPrivileges`).
- `fail2ban-sshd.local` — sshd jail for the controller itself.

## Reaching the UI

There is no public listener, no nginx, no TLS and no domain on the controller.
Forward the port over SSH from the workstation:

```bash
ssh -N -L 3000:127.0.0.1:3000 <controller>
```

## Secrets

- **Panel API token, vault password** — long-lived, so they belong in a Variable
  Group, which keeps its secrets encrypted at rest.
- **Root password of a brand new VPS** — one-off, so it belongs in a **secret
  survey field** named `bootstrap_ssh_password`. In 2.18.29 `Task.Secret` is
  declared `db:"-"` and is replaced before the task record is written, so a
  survey secret is never stored; it exists only for the duration of the run.
- Either kind reaches Ansible as `--extra-vars` **on the command line**, so it is
  visible in `/proc` while the run lasts. That is why the controller stays
  single-purpose, and why the bootstrap play refuses to run with a password at
  `-vv` or higher.
- The deployer SSH private key belongs in the Key Store, not in the repository.

## Templates

Three, and no others — `Preflight`, `Install / Reconcile Node`, `Verify Node`.
Reconcile is also update and repair. The target goes in the run's *Limit* field,
which in 2.18.29 is a free text field rather than a list built from the inventory;
no dropdown is faked and the node list is not duplicated into a survey. A run
that resolves to more than one host is refused by the playbook unless
`node_allow_bulk=true`. `allow_parallel_tasks` is disabled on Install / Reconcile
only, because it writes to the shared Config Profile; the two read-only templates
may run at any time.

The Semaphore inventory is the node registry and the one piece of state that does
not live in git, so it is what a backup has to cover.
