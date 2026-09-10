#!/usr/bin/env python3
"""Validate the versioned capacity inventory.

Exit codes: 0 clean, 1 blockers found, 2 the file could not be read at all.
Warnings never fail the run on their own; --strict promotes them, which is what
a release gate wants and what a day-to-day edit does not.

    python3 monitoring/validate_capacity.py monitoring/capacity/capacity.yml
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import capacity_model  # noqa: E402  (path set above)
import strict_yaml  # noqa: E402  (path set above)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=pathlib.Path, help="capacity inventory to validate")
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures")
    parser.add_argument("--quiet", action="store_true", help="print findings only")
    arguments = parser.parse_args(argv)

    try:
        # The strict loader, not yaml.safe_load: duplicate keys, NaN, Infinity,
        # negatives and quoted numbers are refused here rather than becoming a
        # capacity figure nobody can explain.
        document = strict_yaml.load(arguments.path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"capacity inventory not found: {arguments.path}", file=sys.stderr)
        return 2
    except strict_yaml.StrictYamlError as error:
        print(f"capacity inventory refused: {error}", file=sys.stderr)
        return 2

    inventory = capacity_model.validate(document)
    for problem in inventory.problems:
        stream = sys.stderr if problem.severity == capacity_model.BLOCKER else sys.stdout
        print(problem, file=stream)

    if inventory.blockers:
        print(f"{len(inventory.blockers)} blocker(s) in {arguments.path}", file=sys.stderr)
        return 1
    if arguments.strict and inventory.warnings:
        print(f"{len(inventory.warnings)} warning(s) and --strict was asked for", file=sys.stderr)
        return 1

    if not arguments.quiet:
        active = inventory.active_nodes({name: {"is_connected": True, "is_disabled": False} for name in inventory.nodes})
        print(
            f"{arguments.path}: {len(inventory.nodes)} nodes, {len(inventory.pools)} pools, "
            f"{len(inventory.bridges)} bridges, {len(inventory.warnings)} warning(s). "
            f"Service capacity with every node reachable: "
            f"{inventory.service_capacity('download', active):.0f} Mbit/s down / "
            f"{inventory.service_capacity('upload', active):.0f} Mbit/s up. "
            f"Physical capacity, every leg counted: "
            f"{inventory.physical_capacity('download', active):.0f} / "
            f"{inventory.physical_capacity('upload', active):.0f} Mbit/s (diagnostic only)."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
