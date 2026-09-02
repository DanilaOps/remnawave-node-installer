#!/usr/bin/env python3
"""Turn iperf3 measurements into a rated capacity, deterministically.

This is the arithmetic half of the auto-capacity test: no network, no iperf, no
Ansible. The node-side role runs iperf3 against several Russian endpoints, in
both directions, several times, and hands the throughputs it collected to this
module. Everything that decides what number a node is rated at - which field of
the iperf3 JSON to trust, how a city's runs become one figure, how the cities
become one figure, the safety margin and the rounding - lives here, where it can
be tested against fixtures rather than against a public server that is slow on a
Tuesday.

Two rules run through it, and both exist because the alternative quietly
understates a good link:

  * BETWEEN CITIES, THE BEST ROUTE WINS. A node's capacity to Russia is the best
    route it actually has, not the average and never the worst. Tyumen is often
    much slower than Moscow; taking the minimum or the mean would rate a 8 Gbit/s
    node at 2 Gbit/s because one distant city was congested. So per city we take
    the median of its runs (stable, not a lucky spike), and across cities we take
    the maximum of those medians.

  * A MEASUREMENT IS NOT A GUESS. A rating is published only when enough cities
    agreed to call it real - min_valid_cities per direction. Below that the node
    stays unmeasured: visible, contributing nothing, never handed an invented
    number. The same honesty the capacity file keeps.

Direction. iperf3 names the two ends "sender" and "receiver", and which is the
node flips with -R, so the names are not trusted. Instead the caller tags each
result with the direction it asked for - a normal test is the node uploading to
Russia, a -R test is the node downloading from it - and this module reads the
throughput at the receiving end (sum_received), which is the data that actually
arrived. See parse_bits_per_second and the tests around it.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from typing import Any

DIRECTIONS = ("download", "upload")

# What a normal and a reverse iperf3 run measure, from the node's point of view.
# A normal run has the node as the client sending to the Russian server, so it
# measures the node's UPLOAD. -R reverses it: the server sends, the node
# receives, which measures the node's DOWNLOAD. The direction is the flag we
# passed, not a label in the JSON.
NORMAL_DIRECTION = "upload"
REVERSE_DIRECTION = "download"


def parse_bits_per_second(payload: Any) -> float | None:
    """The throughput one iperf3 run actually delivered, or None if it did not.

    Reads end.sum_received.bits_per_second - the data the receiving end got,
    which is the honest figure in both directions: in a normal run the Russian
    server received the node's upload, in a -R run the node received the
    download. sum_sent is what a sender pushed, including bytes that never
    arrived, so it is not what a link can carry.

    None for anything that is not a completed measurement: an iperf3 error object
    ({"error": "..."}), a truncated or non-JSON body, a missing field, or a
    non-positive rate. The caller treats None as "this run did not count".
    """
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            return None
    if not isinstance(payload, dict):
        return None
    if payload.get("error"):
        return None
    end = payload.get("end")
    if not isinstance(end, dict):
        return None
    received = end.get("sum_received")
    if not isinstance(received, dict):
        return None
    bps = received.get("bits_per_second")
    if not isinstance(bps, (int, float)) or isinstance(bps, bool):
        return None
    if bps != bps or bps in (float("inf"), float("-inf")) or bps <= 0:
        return None
    return float(bps)


def mbps(payload: Any) -> float | None:
    """parse_bits_per_second as Mbit/s, the unit the capacity model speaks."""
    bits = parse_bits_per_second(payload)
    return None if bits is None else bits / 1_000_000.0


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _round_down(value: float, step: int) -> int:
    """Round down to a multiple of step. Never up: an over-stated capacity reads
    as free headroom that is not there."""
    if step <= 0:
        return int(math.floor(value))
    return int(math.floor(value / step) * step)


def city_median(runs: list[Any]) -> float | None:
    """The stable figure for one city and one direction.

    runs is the list of Mbit/s from that city's successful measurements (the
    caller has already dropped the failures). The median, so a single congested
    or lucky pass does not decide the city's number. None when the city produced
    no successful run at all - a dead city, ignored rather than counted as zero.
    """
    numbers = [float(r) for r in runs if isinstance(r, (int, float)) and not isinstance(r, bool) and r > 0]
    if not numbers:
        return None
    return _median(numbers)


def analyze(
    cities: dict[str, dict[str, list[Any]]],
    *,
    safety_factor: float = 0.90,
    round_down_mbps: int = 100,
    min_valid_cities: int = 2,
) -> dict[str, Any]:
    """Rate a node from its per-city, per-direction measurements.

    cities maps a city name to {"download": [mbps, ...], "upload": [mbps, ...]},
    holding only the runs that succeeded. Returns:

        {
          "certain": bool,
          "download_mbps": int | None,
          "upload_mbps": int | None,
          "valid_cities": {"download": int, "upload": int},
          "cities": {name: {"download": median|None, "upload": median|None}},
          "best": {"download": float|None, "upload": float|None},
          "rated_raw": {"download": float|None, "upload": float|None},
        }

    certain is true only when BOTH directions had at least min_valid_cities
    cities with a successful measurement. When it is false the node is
    unmeasured: download_mbps and upload_mbps are both None, whatever the numbers
    were, because a figure half the fleet could not confirm is not published.
    """
    per_city: dict[str, dict[str, float | None]] = {}
    medians: dict[str, list[float]] = {"download": [], "upload": []}
    for name, directions in cities.items():
        directions = directions if isinstance(directions, dict) else {}
        per_city[name] = {}
        for direction in DIRECTIONS:
            median = city_median(directions.get(direction) or [])
            per_city[name][direction] = median
            if median is not None:
                medians[direction].append(median)

    valid = {direction: len(medians[direction]) for direction in DIRECTIONS}
    best = {
        direction: (max(medians[direction]) if medians[direction] else None)
        for direction in DIRECTIONS
    }
    rated_raw = {
        direction: (best[direction] * safety_factor if best[direction] is not None else None)
        for direction in DIRECTIONS
    }

    certain = all(valid[direction] >= min_valid_cities for direction in DIRECTIONS)
    result: dict[str, Any] = {
        "certain": certain,
        "download_mbps": None,
        "upload_mbps": None,
        "valid_cities": valid,
        "cities": per_city,
        "best": best,
        "rated_raw": rated_raw,
        "config": {
            "safety_factor": safety_factor,
            "round_down_mbps": round_down_mbps,
            "min_valid_cities": min_valid_cities,
        },
    }
    if certain:
        result["download_mbps"] = _round_down(rated_raw["download"], round_down_mbps)
        result["upload_mbps"] = _round_down(rated_raw["upload"], round_down_mbps)
    return result


def summary_text(node: str, result: dict[str, Any]) -> str:
    """A short, human-readable account of how the rating was reached."""
    lines = [f"Capacity auto-test: {node}"]
    for direction in DIRECTIONS:
        lines.append("")
        lines.append(direction.upper())
        for name in sorted(result["cities"]):
            median = result["cities"][name][direction]
            lines.append(f"  {name}: {'median %.0f' % median if median is not None else 'no result'}")
        best = result["best"][direction]
        raw = result["rated_raw"][direction]
        lines.append(f"  best stable: {'%.0f' % best if best is not None else 'n/a'}")
        lines.append(f"  safety {result['config']['safety_factor']:.2f}: {'%.0f' % raw if raw is not None else 'n/a'}")
        rated = result["download_mbps"] if direction == "download" else result["upload_mbps"]
        lines.append(f"  rated: {rated if rated is not None else 'N/A (unmeasured)'} Mbit/s")
    lines.append("")
    lines.append(f"capacity_certain={'true' if result['certain'] else 'false'} "
                 f"(valid cities: download {result['valid_cities']['download']}, "
                 f"upload {result['valid_cities']['upload']}, "
                 f"need {result['config']['min_valid_cities']})")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--measurements", type=str, required=True, help="path to the collected cities JSON, or - for stdin")
    parser.add_argument("--node", default="node", help="node name, for the summary")
    parser.add_argument("--safety-factor", type=float, default=0.90)
    parser.add_argument("--round-down-mbps", type=int, default=100)
    parser.add_argument("--min-valid-cities", type=int, default=2)
    arguments = parser.parse_args(argv)

    raw = sys.stdin.read() if arguments.measurements == "-" else open(arguments.measurements, encoding="utf-8").read()
    try:
        cities = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as error:
        print(f"measurements are not valid JSON: {error}", file=sys.stderr)
        return 2
    if not isinstance(cities, dict):
        print("measurements must be an object of city -> {download, upload}", file=sys.stderr)
        return 2

    result = analyze(
        cities,
        safety_factor=arguments.safety_factor,
        round_down_mbps=arguments.round_down_mbps,
        min_valid_cities=arguments.min_valid_cities,
    )
    print(summary_text(arguments.node, result), file=sys.stderr)
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
