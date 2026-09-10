#!/usr/bin/env python3
"""Merge inventory-declared node capacity into the deployed capacity inventory.

The problem this solves: a node's capacity used to be typed twice - once in the
Ansible inventory where the node is defined, and again by hand in
monitoring/capacity/capacity.yml. This tool makes the Ansible inventory the
single place a standard node's capacity is written. The installer collects the
capacity fields from the inventory and this tool folds them into the capacity
inventory the exporter reads, so the second, manual edit disappears.

What it is NOT: it does not invent capacity, it does not touch pools or bridges
that the inventory says nothing about, and it never writes an address. The
inventory carries a node's name and its rated Mbit/s; a machine's address stays
in the Ansible inventory and /etc/remnawave and never reaches here - the same
"no secrets in capacity" rule the committed file keeps.

Inputs:
  --base            The curated capacity.yml: pools, bridges, and every node
                    that is NOT managed from the inventory. Loaded with the same
                    strict loader the exporter uses, so a malformed base is
                    refused rather than silently merged.
  --inventory-json  A JSON array of the inventory-managed nodes, each:
                        {"name": "TR-02", "pool": "TR",
                         "download_mbps": 1000, "upload_mbps": 1000,
                         "certain": true}
                    name/pool are derived by Ansible from the inventory
                    hostname; download_mbps/upload_mbps/certain come from the
                    inventory host. A node with certain=false, or with a missing
                    figure, is published unmeasured rather than counted.
  --output          Where the merged, validated inventory is written. Default
                    stdout. The merged document is machine-generated on purpose:
                    the reviewable, commented source is --base plus the Ansible
                    inventory, and this output is rebuilt from them.

The merge is refused - nothing is written - if the result would not validate,
or if a node with a real per-node figure is placed in a pool that already
carries an aggregate location figure. A pool cannot be rated as one location and
also hold a member that publishes its own capacity: the same gigabits would be
counted twice. The message says how to resolve it.

Exit codes: 0 merged and valid, 1 a blocker (validation or a pool conflict),
2 an input could not be read.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import capacity_model  # noqa: E402  (path set above)
import strict_yaml  # noqa: E402  (path set above)

try:  # PyYAML is already a dependency of the monitoring tests and the exporter.
    import yaml
except ImportError:  # pragma: no cover - environment without PyYAML
    yaml = None


class SyncError(Exception):
    """A reason the merge must not be written."""


def _number(value: object, field: str, node: str) -> float | int:
    """A capacity figure, kept an int when the operator wrote one.

    The strict loader refuses a quoted number and a negative one in the base;
    the same has to hold for a figure coming from the inventory, or the
    inventory becomes a way around the rules the committed file keeps.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SyncError(f"node {node}: {field} must be a number, got {value!r}")
    if value < 0:
        raise SyncError(f"node {node}: {field} is negative ({value})")
    if value != value or value in (float("inf"), float("-inf")):  # NaN / Inf
        raise SyncError(f"node {node}: {field} is not a finite number")
    # 1000.0 -> 1000 so the written file reads the way a human would type it.
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _capacity_side(mbps: object, certain: bool, field: str, node: str) -> dict:
    """One direction's capacity block.

    certain and a figure -> a declared per-node rating that counts. Anything
    else -> unmeasured, which is published as "we do not know" and contributes
    nothing, rather than a number the operator did not stand behind.
    """
    if certain and mbps is not None:
        return {"mbps": _number(mbps, field, node), "source": "declared"}
    return {"source": "unmeasured"}


def _node_entry(item: dict) -> tuple[str, str, dict, bool]:
    """(name, pool, capacity entry, is_rated) for one inventory node."""
    name = str(item.get("name", "")).strip()
    pool = str(item.get("pool", "")).strip()
    if not name:
        raise SyncError("an inventory node has no name")
    if not pool:
        raise SyncError(f"node {name}: no pool (Ansible derives it from the country code; set capacity_pool to override)")
    certain = bool(item.get("certain", False))
    download = item.get("download_mbps")
    upload = item.get("upload_mbps")
    down_block = _capacity_side(download, certain, "capacity_download_mbps", name)
    up_block = _capacity_side(upload, certain, "capacity_upload_mbps", name)
    is_rated = down_block.get("source") == "declared" or up_block.get("source") == "declared"
    entry = {
        "pool": pool,
        "enabled": bool(item.get("enabled", True)),
        "capacity": {"download": down_block, "upload": up_block},
        # A node is measured by its own inbound counters; the model refuses an
        # outbound measurement on a node, which would describe a bridge.
        "measurement": {"kind": "inbound", "node": name},
    }
    return name, pool, entry, is_rated


def merge(base: dict, inventory_nodes: list[dict]) -> dict:
    """Fold the inventory nodes into a copy of the base document."""
    if not isinstance(base, dict):
        raise SyncError("base capacity inventory is not a mapping")
    document = json.loads(json.dumps(base))  # deep copy without importing copy
    nodes = document.setdefault("nodes", {})
    pools = document.setdefault("pools", {})
    if not isinstance(nodes, dict) or not isinstance(pools, dict):
        raise SyncError("base capacity inventory has a non-mapping nodes/pools section")

    for item in inventory_nodes:
        if not isinstance(item, dict):
            raise SyncError(f"inventory node is not an object: {item!r}")
        name, pool, entry, is_rated = _node_entry(item)
        # Inventory wins: a node managed from the inventory replaces whatever the
        # base said about it, so there is one source of truth for its figure.
        nodes[name] = entry

        # Its pool follows it. If the base listed the node in a different pool,
        # drop it from there - a node belongs to exactly one pool, and leaving it
        # in the old one as well is a validation blocker, not a merge.
        for other_name, other in pools.items():
            if other_name == pool or not isinstance(other, dict):
                continue
            members = other.get("members")
            if isinstance(members, list) and name in members:
                other["members"] = [m for m in members if m != name]

        existing = pools.get(pool)
        if existing is None:
            # A brand-new location. It carries no aggregate figure - its
            # members rate themselves - which is exactly the shape the model
            # allows a rated member to sit in.
            pools[pool] = {"strategy": "leastLoad", "members": [name]}
            continue
        if not isinstance(existing, dict):
            raise SyncError(f"pool {pool!r} in the base is not a mapping")
        if is_rated and existing.get("capacity"):
            raise SyncError(
                f"node {name} publishes its own capacity but pool {pool!r} already carries an "
                f"aggregate figure. A pool rated as one location cannot also hold a member that "
                f"rates itself - the same capacity would be counted twice. Put {name} in a "
                f"per-node pool (set capacity_pool on the host), or drop the aggregate figure from "
                f"pool {pool!r} in the base capacity.yml."
            )
        members = existing.setdefault("members", [])
        if not isinstance(members, list):
            raise SyncError(f"pool {pool!r} members is not a list")
        if name not in members:
            members.append(name)

    return document


def _validate(document: dict) -> capacity_model.CapacityInventory:
    inventory = capacity_model.validate(document)
    if inventory.blockers:
        lines = "\n".join(f"  {problem}" for problem in inventory.blockers)
        raise SyncError(f"the merged capacity inventory is invalid:\n{lines}")
    return inventory


def _dump(document: dict) -> str:
    if yaml is None:  # pragma: no cover
        raise SyncError("PyYAML is required to write the merged inventory")
    header = (
        "---\n"
        "# GENERATED - do not edit by hand.\n"
        "#\n"
        "# Rebuilt by monitoring/sync_capacity.py from the curated base capacity\n"
        "# inventory and the Ansible inventory. A standard node's rated capacity is\n"
        "# declared once, on the host in the Ansible inventory; this file is the\n"
        "# merge the exporter reads. Edit the base or the inventory, not this.\n"
    )
    body = yaml.safe_dump(document, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return header + body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", type=pathlib.Path, required=True, help="curated base capacity.yml")
    parser.add_argument(
        "--inventory-json",
        type=pathlib.Path,
        required=True,
        help="JSON array of inventory-managed nodes, or - for stdin",
    )
    parser.add_argument("--output", type=pathlib.Path, default=None, help="where to write the merge (default stdout)")
    parser.add_argument("--check", action="store_true", help="validate the merge but write nothing")
    arguments = parser.parse_args(argv)

    try:
        base = strict_yaml.load(arguments.base.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"base capacity inventory not found: {arguments.base}", file=sys.stderr)
        return 2
    except strict_yaml.StrictYamlError as error:
        print(f"base capacity inventory refused: {error}", file=sys.stderr)
        return 2

    try:
        raw = sys.stdin.read() if str(arguments.inventory_json) == "-" else arguments.inventory_json.read_text(encoding="utf-8")
        inventory_nodes = json.loads(raw) if raw.strip() else []
    except FileNotFoundError:
        print(f"inventory JSON not found: {arguments.inventory_json}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as error:
        print(f"inventory JSON is not valid JSON: {error}", file=sys.stderr)
        return 2
    if not isinstance(inventory_nodes, list):
        print("inventory JSON must be an array of node objects", file=sys.stderr)
        return 2

    try:
        document = merge(base, inventory_nodes)
        inventory = _validate(document)
    except SyncError as error:
        print(str(error), file=sys.stderr)
        return 1

    managed = sorted(str(item.get("name")) for item in inventory_nodes if isinstance(item, dict))
    print(
        f"merged {len(managed)} inventory-managed node(s) "
        f"[{', '.join(managed) if managed else 'none'}] into "
        f"{len(inventory.nodes)} nodes / {len(inventory.pools)} pools / {len(inventory.bridges)} bridges",
        file=sys.stderr,
    )

    if arguments.check:
        return 0

    rendered = _dump(document)
    if arguments.output is None:
        sys.stdout.write(rendered)
    else:
        arguments.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
