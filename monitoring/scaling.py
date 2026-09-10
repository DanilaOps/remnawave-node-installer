"""Where the next node should go, and the arithmetic behind saying so.

This module is the specification. The PromQL in
monitoring/prometheus/recording-rules.yml implements the same
formula for the dashboard, and the tests here are what pin it down - a ranking
that lives only in a PromQL expression cannot be tested without a running
Prometheus, and a recommendation nobody can test is a recommendation nobody
should act on.

The order is fixed:

    1  RED bridge            a bridge under the RED threshold
    2  RED pool              a pool under the RED threshold
    3  YELLOW bridge
    4  YELLOW pool
    5  overloaded node       an active node under the RED threshold

A bridge outranks a pool at the same colour on purpose: a bridge is a single
link with no redundancy, and adding a node to the pool behind it does not widen
it. Ties are broken in this order, and the last one exists only so that the
answer never depends on dictionary order:

    1  lowest free percentage
    2  lowest absolute free capacity
    3  highest growth
    4  name, ascending

Forecasting is deliberately conservative. A trend computed from too few samples,
from a flat series, from a falling one or across a counter reset is refused
rather than turned into a date.
"""

from __future__ import annotations

import dataclasses
import math
from typing import Iterable, Sequence

# Priorities, lowest acts first. Written out rather than computed so that
# changing the order is a visible edit.
PRIORITY_RED_BRIDGE = 1
PRIORITY_RED_POOL = 2
PRIORITY_YELLOW_BRIDGE = 3
PRIORITY_YELLOW_POOL = 4
PRIORITY_OVERLOADED_NODE = 5

RED = "red"
YELLOW = "yellow"
GREEN = "green"

# Bounds used to fold the tie-breaks into one scalar. Each term is smaller than
# the one above it by construction, so a worse priority can never be rescued by
# a tie-break and a bigger free ratio can never be rescued by absolute capacity.
_PRIORITY_WEIGHT = 1e9
_RATIO_WEIGHT = 1e6
_FREE_WEIGHT = 1e3
_FREE_CAP_MBPS = 999.0
_GROWTH_CAP = 999.0


def _finite(value: float, fallback: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(number):
        return fallback
    return number


@dataclasses.dataclass(frozen=True)
class Candidate:
    """One thing that could get the next node."""

    kind: str          # "bridge" | "pool" | "node"
    name: str
    free_ratio: float  # 0..1, the worse of the two directions
    free_mbps: float
    growth_ratio: float = 1.0
    active: bool = True
    # A pool whose capacity is an aggregate with a member missing has a free
    # ratio that is an upper bound. It still competes, and the fact travels with
    # it so the recommendation can say so.
    certain: bool = True

    def status(self, red_below: float, green_at: float) -> str:
        # A non-finite ratio is read as zero, which is RED. NaN compares false
        # against everything, so the naive form would silently classify a broken
        # input as GREEN - the one direction of error that hides a problem
        # instead of surfacing it.
        ratio = _finite(self.free_ratio, 0.0)
        if ratio < red_below:
            return RED
        if ratio < green_at:
            return YELLOW
        return GREEN


def priority(candidate: Candidate, red_below: float, green_at: float) -> int | None:
    """The candidate's place in the fixed order, or None if it does not qualify.

    GREEN never qualifies: nothing needs a node yet. An inactive node never
    qualifies either - a machine nobody is using has no interesting headroom,
    and recommending capacity for it would be recommending capacity for a
    machine that is down.
    """
    status = candidate.status(red_below, green_at)
    if status == GREEN:
        return None
    if candidate.kind == "bridge":
        return PRIORITY_RED_BRIDGE if status == RED else PRIORITY_YELLOW_BRIDGE
    if candidate.kind == "pool":
        return PRIORITY_RED_POOL if status == RED else PRIORITY_YELLOW_POOL
    if candidate.kind == "node":
        # Only an overloaded active node, and only at RED. A yellow node inside
        # a green pool is not a scaling signal: the pool has room.
        if status == RED and candidate.active:
            return PRIORITY_OVERLOADED_NODE
        return None
    raise ValueError(f"unknown candidate kind {candidate.kind!r}")


def rank(candidate: Candidate, red_below: float, green_at: float) -> float | None:
    """One scalar that encodes the whole order. Lower acts first.

    Returned as a number rather than a tuple so the identical formula can be
    written in PromQL for the dashboard.
    """
    place = priority(candidate, red_below, green_at)
    if place is None:
        return None
    free_ratio = min(max(_finite(candidate.free_ratio), 0.0), 1.0)
    free_mbps = min(max(_finite(candidate.free_mbps), 0.0), _FREE_CAP_MBPS)
    growth = min(max(_finite(candidate.growth_ratio, 1.0), 0.0), _GROWTH_CAP)
    return (
        place * _PRIORITY_WEIGHT
        + free_ratio * _RATIO_WEIGHT
        + free_mbps * _FREE_WEIGHT
        - growth
    )


def _sort_key(candidate: Candidate, red_below: float, green_at: float) -> tuple:
    place = priority(candidate, red_below, green_at)
    assert place is not None  # filtered before this is called
    return (
        place,
        min(max(_finite(candidate.free_ratio), 0.0), 1.0),
        min(max(_finite(candidate.free_mbps), 0.0), _FREE_CAP_MBPS),
        -min(max(_finite(candidate.growth_ratio, 1.0), 0.0), _GROWTH_CAP),
        candidate.name,
    )


def order(candidates: Iterable[Candidate], red_below: float, green_at: float) -> list[Candidate]:
    """Every candidate that needs a node, worst first.

    Sorted on the explicit tuple rather than on rank(): the scalar exists for
    PromQL, and folding four criteria into one float can lose the last bits of
    the smallest term. The tuple is the authority, and
    ``test_scaling.py`` checks the two agree wherever the scalar can express it.
    """
    qualifying = [c for c in candidates if priority(c, red_below, green_at) is not None]
    return sorted(qualifying, key=lambda c: _sort_key(c, red_below, green_at))


def recommend(candidates: Iterable[Candidate], red_below: float, green_at: float) -> Candidate | None:
    """The single next action, or None when nothing needs one."""
    ordered = order(candidates, red_below, green_at)
    return ordered[0] if ordered else None


# --- forecasting -------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Trend:
    """The outcome of trying to forecast. Never a bare number."""

    verdict: str                    # "ok" | "insufficient_history" | "flat" | "falling" | "no_data" | "reset"
    slope_bps_per_second: float = 0.0
    seconds_to_threshold: float | None = None

    @property
    def usable(self) -> bool:
        return self.verdict == "ok"


def growth_ratio(short_average: float, long_average: float, samples: int, min_samples: int) -> float | None:
    """Short window over long window, or None when the answer would be noise.

    None for too little history and for a zero baseline. A pool that was idle
    yesterday and is busy today does not have infinite growth; it has a baseline
    nobody can divide by.
    """
    if samples < min_samples:
        return None
    long_average = _finite(long_average, 0.0)
    if long_average <= 0:
        return None
    return _finite(short_average, 0.0) / long_average


def forecast(
    samples: Sequence[tuple[float, float]],
    capacity_bps: float,
    red_below: float,
    min_samples: int,
    min_slope_bps_per_second: float = 1.0,
) -> Trend:
    """Least squares over (timestamp, value), with every refusal named.

    Handles, and says which happened: no data at all, too few samples, a flat
    series, a falling series, a counter reset in the window, and a capacity of
    zero or less. What it never does is return a date derived from one of those.
    """
    if not samples:
        return Trend("no_data")
    if len(samples) < min_samples:
        return Trend("insufficient_history")
    values = [value for _time, value in samples]
    # A drop of more than half in one step is a restart, not a trend. Fitting
    # across it produces a slope that describes the restart.
    for previous, current in zip(values, values[1:]):
        if previous > 0 and current < previous / 2:
            return Trend("reset")
    capacity_bps = _finite(capacity_bps, 0.0)
    if capacity_bps <= 0:
        return Trend("no_data")

    count = len(samples)
    mean_time = sum(t for t, _v in samples) / count
    mean_value = sum(values) / count
    numerator = sum((t - mean_time) * (v - mean_value) for t, v in samples)
    denominator = sum((t - mean_time) ** 2 for t, _v in samples)
    if denominator <= 0:
        return Trend("insufficient_history")
    slope = numerator / denominator
    if abs(slope) < min_slope_bps_per_second:
        return Trend("flat", slope)
    if slope < 0:
        return Trend("falling", slope)

    threshold_bps = capacity_bps * (1.0 - red_below)
    current = values[-1]
    remaining = threshold_bps - current
    if remaining <= 0:
        # Already past it. Zero, not a negative date.
        return Trend("ok", slope, 0.0)
    return Trend("ok", slope, remaining / slope)
