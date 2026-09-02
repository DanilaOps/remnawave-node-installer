#!/usr/bin/env python3
"""Run the iperf3 measurements on a node and collect the throughputs.

This is the half that touches the network - it runs on the new node, calls
iperf3 against the Russian endpoints and collects the raw Mbit/s. It deliberately
does no arithmetic: the median, the maximum across cities, the safety factor and
the rounding all live in iperf_capacity.py, where they are tested against
fixtures. This file's only job is to turn a list of endpoints into
{city: {download: [...], upload: [...]}}, dropping whatever failed.

What it is careful about, because public iperf3 servers are:

  * busy, unreachable, rate limited or simply down. A city that yields nothing is
    left out, not counted as zero, and the run continues with the others.
  * on one of several ports (5201-5209) and behind a fallback host. It probes for
    a reachable port and moves to the fallback if the primary does not answer.
  * capable of returning an error object or a truncated body instead of a
    measurement. iperf_capacity.mbps returns None for those, and a None run does
    not count.

Directions, from the node's point of view: a normal run is the node uploading to
Russia; -R is the node downloading from it. The direction is the flag, never a
label in the JSON.

Every part that touches the outside world - the iperf3 call and the port probe -
is injectable, so the collection logic is tested without a server. The defaults
shell out to iperf3 and open a socket; the tests pass fakes.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import socket
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import iperf_capacity  # noqa: E402


def default_iperf(host: str, port: int, streams: int, duration: int, reverse: bool, timeout: float) -> str:
    """Run one iperf3 test and return its stdout, or '' on any failure.

    Measures the existing network path and nothing else: no tunnel, no routing
    change, no tuning. -R makes the server send (the node's download); without it
    the node sends (its upload).
    """
    argv = ["iperf3", "-c", host, "-p", str(port), "-P", str(streams), "-t", str(duration), "-J"]
    if reverse:
        argv.append("-R")
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    # iperf3 prints its JSON (including an error object) to stdout even on a
    # non-zero exit, so the body is parsed regardless of the return code.
    return completed.stdout or ""


def default_probe(host: str, port: int, timeout: float) -> bool:
    """True when host:port accepts a TCP connection within timeout."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _find_target(hosts: list[dict], probe, connect_timeout: float) -> tuple[str, int] | None:
    """The first reachable (host, port) across the primary and its fallbacks."""
    for entry in hosts:
        host = entry.get("host")
        if not host:
            continue
        for port in entry.get("ports") or [5201]:
            if probe(host, int(port), connect_timeout):
                return host, int(port)
    return None


def _measure(host: str, port: int, direction: str, config: dict, iperf) -> list[float]:
    """Up to config['runs'] successful Mbit/s for one direction."""
    reverse = direction == iperf_capacity.REVERSE_DIRECTION  # download uses -R
    wanted = int(config.get("runs", 3))
    # A few extra attempts so a single busy pass does not lose a run, but bounded
    # so a wedged server cannot make the test run forever.
    budget = wanted + int(config.get("retries", 1))
    got: list[float] = []
    attempts = 0
    while len(got) < wanted and attempts < budget:
        attempts += 1
        body = iperf(
            host,
            port,
            int(config.get("streams", 8)),
            int(config.get("duration_seconds", 10)),
            reverse,
            float(config.get("run_timeout_seconds", 25)),
        )
        rate = iperf_capacity.mbps(body)
        if rate is not None:
            got.append(rate)
    return got


def collect(config: dict, *, iperf=default_iperf, probe=default_probe) -> dict[str, dict[str, list[float]]]:
    """Measure every endpoint and return {city: {download: [...], upload: [...]}}.

    A city with no reachable port, or no successful run in either direction, is
    absent from the result - the caller counts valid cities, and an absent one is
    simply not valid.
    """
    connect_timeout = float(config.get("connect_timeout_seconds", 5))
    result: dict[str, dict[str, list[float]]] = {}
    for endpoint in config.get("endpoints", []):
        city = endpoint.get("city")
        if not city:
            continue
        target = _find_target(endpoint.get("hosts") or [], probe, connect_timeout)
        if target is None:
            continue  # dead city: unreachable on every host and port
        host, port = target
        download = _measure(host, port, iperf_capacity.REVERSE_DIRECTION, config, iperf)
        upload = _measure(host, port, iperf_capacity.NORMAL_DIRECTION, config, iperf)
        if download or upload:
            result[city] = {"download": download, "upload": upload}
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True, help="path to the endpoints/params JSON, or - for stdin")
    parser.add_argument("--output", default=None, help="where to write the collected measurements (default stdout)")
    arguments = parser.parse_args(argv)

    raw = sys.stdin.read() if arguments.config == "-" else open(arguments.config, encoding="utf-8").read()
    config = json.loads(raw)
    measurements = collect(config)
    rendered = json.dumps(measurements)
    if arguments.output:
        open(arguments.output, "w", encoding="utf-8").write(rendered + "\n")
    else:
        sys.stdout.write(rendered + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
