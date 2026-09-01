"""The next-node recommendation, priority by priority and tie-break by tie-break.

A recommendation that changes between two runs on the same data is worse than no
recommendation: somebody acts on it, and the next person sees something else. So
every level of the order has a fixture, the tie-breaks are exercised in
isolation, and the whole thing is checked to be independent of input order.
"""

from __future__ import annotations

import itertools
import pathlib
import random
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scaling  # noqa: E402

RED_BELOW = 0.20
GREEN_AT = 0.30


def candidate(kind: str, name: str, free_ratio: float, **kwargs) -> scaling.Candidate:
    return scaling.Candidate(kind=kind, name=name, free_ratio=free_ratio, free_mbps=kwargs.pop("free_mbps", 100.0), **kwargs)


class PriorityTests(unittest.TestCase):
    def test_a_green_candidate_never_qualifies(self) -> None:
        for kind in ("bridge", "pool", "node"):
            with self.subTest(kind=kind):
                self.assertIsNone(scaling.priority(candidate(kind, "X", 0.55), RED_BELOW, GREEN_AT))

    def test_the_five_priorities(self) -> None:
        expected = [
            (candidate("bridge", "B", 0.05), scaling.PRIORITY_RED_BRIDGE),
            (candidate("pool", "P", 0.05), scaling.PRIORITY_RED_POOL),
            (candidate("bridge", "B", 0.25), scaling.PRIORITY_YELLOW_BRIDGE),
            (candidate("pool", "P", 0.25), scaling.PRIORITY_YELLOW_POOL),
            (candidate("node", "N", 0.05), scaling.PRIORITY_OVERLOADED_NODE),
        ]
        for item, place in expected:
            with self.subTest(kind=item.kind, ratio=item.free_ratio):
                self.assertEqual(place, scaling.priority(item, RED_BELOW, GREEN_AT))

    def test_a_yellow_node_is_not_a_scaling_signal(self) -> None:
        # Its pool has room; the node is simply the busiest member of it.
        self.assertIsNone(scaling.priority(candidate("node", "N", 0.25), RED_BELOW, GREEN_AT))

    def test_an_inactive_node_never_qualifies(self) -> None:
        # Recommending capacity for a machine that is down is recommending the
        # wrong thing.
        self.assertIsNone(
            scaling.priority(candidate("node", "N", 0.01, active=False), RED_BELOW, GREEN_AT)
        )

    def test_the_full_order_is_the_declared_one(self) -> None:
        candidates = [
            candidate("node", "node-red", 0.01),
            candidate("pool", "pool-yellow", 0.25),
            candidate("bridge", "bridge-yellow", 0.25),
            candidate("pool", "pool-red", 0.01),
            candidate("bridge", "bridge-red", 0.01),
            candidate("pool", "pool-green", 0.90),
        ]
        ordered = [item.name for item in scaling.order(candidates, RED_BELOW, GREEN_AT)]
        self.assertEqual(
            ["bridge-red", "pool-red", "bridge-yellow", "pool-yellow", "node-red"], ordered
        )

    def test_a_bridge_outranks_a_pool_at_the_same_colour(self) -> None:
        # A bridge is one link with no redundancy, and adding a node to the pool
        # behind it does not widen it.
        chosen = scaling.recommend(
            [candidate("pool", "P", 0.01, free_mbps=1.0), candidate("bridge", "B", 0.19, free_mbps=900.0)],
            RED_BELOW,
            GREEN_AT,
        )
        self.assertEqual("B", chosen.name)

    def test_nothing_to_do_returns_none(self) -> None:
        self.assertIsNone(scaling.recommend([candidate("pool", "P", 0.80)], RED_BELOW, GREEN_AT))

    def test_an_unknown_kind_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            scaling.priority(candidate("datacentre", "X", 0.01), RED_BELOW, GREEN_AT)


class TieBreakTests(unittest.TestCase):
    def test_first_tie_break_is_the_lowest_free_percentage(self) -> None:
        chosen = scaling.recommend(
            [
                candidate("pool", "roomier", 0.15, free_mbps=1.0),
                candidate("pool", "tighter", 0.05, free_mbps=900.0),
            ],
            RED_BELOW,
            GREEN_AT,
        )
        self.assertEqual("tighter", chosen.name)

    def test_second_tie_break_is_the_lowest_absolute_free_capacity(self) -> None:
        chosen = scaling.recommend(
            [
                candidate("pool", "big", 0.10, free_mbps=500.0, growth_ratio=9.0),
                candidate("pool", "small", 0.10, free_mbps=50.0, growth_ratio=1.0),
            ],
            RED_BELOW,
            GREEN_AT,
        )
        self.assertEqual("small", chosen.name)

    def test_third_tie_break_is_the_highest_growth(self) -> None:
        chosen = scaling.recommend(
            [
                candidate("pool", "steady", 0.10, free_mbps=100.0, growth_ratio=1.0),
                candidate("pool", "growing", 0.10, free_mbps=100.0, growth_ratio=2.5),
            ],
            RED_BELOW,
            GREEN_AT,
        )
        self.assertEqual("growing", chosen.name)

    def test_the_last_tie_break_makes_the_answer_total(self) -> None:
        # Identical on every declared criterion. Without a final tie-break the
        # answer would depend on input order, which is the one thing a
        # recommendation must not do.
        pair = [
            candidate("pool", "beta", 0.10, free_mbps=100.0, growth_ratio=1.0),
            candidate("pool", "alpha", 0.10, free_mbps=100.0, growth_ratio=1.0),
        ]
        self.assertEqual("alpha", scaling.recommend(pair, RED_BELOW, GREEN_AT).name)
        self.assertEqual("alpha", scaling.recommend(list(reversed(pair)), RED_BELOW, GREEN_AT).name)

    def test_the_order_is_independent_of_input_order(self) -> None:
        candidates = [
            candidate("bridge", "b1", 0.05, free_mbps=10.0),
            candidate("bridge", "b2", 0.05, free_mbps=20.0),
            candidate("pool", "p1", 0.05, free_mbps=10.0, growth_ratio=3.0),
            candidate("pool", "p2", 0.05, free_mbps=10.0, growth_ratio=1.0),
            candidate("node", "n1", 0.01),
        ]
        reference = [item.name for item in scaling.order(candidates, RED_BELOW, GREEN_AT)]
        for permutation in itertools.permutations(candidates):
            self.assertEqual(reference, [item.name for item in scaling.order(permutation, RED_BELOW, GREEN_AT)])

    def test_a_priority_is_never_beaten_by_a_tie_break(self) -> None:
        # A RED pool with almost no free capacity must still lose to a YELLOW
        # bridge? No - the other way round: priority dominates, so the RED pool
        # wins over a YELLOW bridge, and no tie-break can invert that.
        chosen = scaling.recommend(
            [
                candidate("pool", "red-pool", 0.19, free_mbps=998.0, growth_ratio=0.0),
                candidate("bridge", "yellow-bridge", 0.20, free_mbps=0.0, growth_ratio=999.0),
            ],
            RED_BELOW,
            GREEN_AT,
        )
        self.assertEqual("red-pool", chosen.name)

    def test_the_scalar_rank_agrees_with_the_order(self) -> None:
        # The PromQL side of this uses the scalar. Where the values are far
        # enough apart for a float to express, the two must not disagree.
        candidates = [
            candidate("bridge", "b-red", 0.05, free_mbps=10.0),
            candidate("pool", "p-red", 0.05, free_mbps=10.0),
            candidate("bridge", "b-yellow", 0.25, free_mbps=10.0),
            candidate("pool", "p-yellow", 0.25, free_mbps=10.0),
            candidate("node", "n-red", 0.05, free_mbps=10.0),
        ]
        by_order = [item.name for item in scaling.order(candidates, RED_BELOW, GREEN_AT)]
        by_rank = [
            item.name
            for item in sorted(candidates, key=lambda c: scaling.rank(c, RED_BELOW, GREEN_AT))
            if scaling.rank(item, RED_BELOW, GREEN_AT) is not None
        ]
        self.assertEqual(by_order, by_rank)


class RobustnessTests(unittest.TestCase):
    def test_a_nan_free_ratio_does_not_crash_the_ranking(self) -> None:
        chosen = scaling.recommend(
            [candidate("pool", "broken", float("nan")), candidate("pool", "real", 0.10)],
            RED_BELOW,
            GREEN_AT,
        )
        # NaN falls back to 0, which is RED, and 0 is the lowest free ratio - so
        # a broken input is loud rather than invisible.
        self.assertEqual("broken", chosen.name)

    def test_an_infinite_free_capacity_is_capped(self) -> None:
        value = scaling.rank(candidate("pool", "P", 0.10, free_mbps=float("inf")), RED_BELOW, GREEN_AT)
        self.assertTrue(value == value)  # not NaN
        self.assertLess(value, scaling.PRIORITY_RED_POOL * 1e9 + 1e9)

    def test_an_uncertain_candidate_still_competes(self) -> None:
        chosen = scaling.recommend(
            [candidate("pool", "aggregate", 0.05, certain=False)], RED_BELOW, GREEN_AT
        )
        self.assertEqual("aggregate", chosen.name)
        self.assertFalse(chosen.certain)


class GrowthTests(unittest.TestCase):
    def test_too_little_history_has_no_growth(self) -> None:
        self.assertIsNone(scaling.growth_ratio(2.0, 1.0, samples=10, min_samples=100))

    def test_a_zero_baseline_has_no_growth(self) -> None:
        # A pool that was idle yesterday does not have infinite growth.
        self.assertIsNone(scaling.growth_ratio(5.0, 0.0, samples=200, min_samples=100))

    def test_growth_is_short_over_long(self) -> None:
        self.assertAlmostEqual(2.0, scaling.growth_ratio(200.0, 100.0, samples=200, min_samples=100))

    def test_shrinking_is_a_number_below_one(self) -> None:
        self.assertAlmostEqual(0.5, scaling.growth_ratio(50.0, 100.0, samples=200, min_samples=100))


class ForecastTests(unittest.TestCase):
    @staticmethod
    def series(values: list[float], step: float = 30.0) -> list[tuple[float, float]]:
        return [(index * step, value) for index, value in enumerate(values)]

    def test_no_data_is_named(self) -> None:
        self.assertEqual("no_data", scaling.forecast([], 1e9, 0.20, min_samples=5).verdict)

    def test_insufficient_history_is_named(self) -> None:
        trend = scaling.forecast(self.series([1.0, 2.0]), 1e9, 0.20, min_samples=10)
        self.assertEqual("insufficient_history", trend.verdict)
        self.assertIsNone(trend.seconds_to_threshold)

    def test_a_flat_series_is_named_and_forecasts_nothing(self) -> None:
        trend = scaling.forecast(self.series([100.0] * 20), 1e9, 0.20, min_samples=5)
        self.assertEqual("flat", trend.verdict)
        self.assertIsNone(trend.seconds_to_threshold)
        self.assertFalse(trend.usable)

    def test_a_falling_series_is_named(self) -> None:
        trend = scaling.forecast(self.series([1000.0 - 30 * i for i in range(20)]), 1e9, 0.20, min_samples=5)
        self.assertEqual("falling", trend.verdict)
        self.assertIsNone(trend.seconds_to_threshold)

    def test_a_counter_reset_is_named(self) -> None:
        # Fitting across a restart produces a slope that describes the restart.
        values = [100.0 * i for i in range(1, 11)] + [10.0, 20.0, 30.0, 40.0, 50.0]
        trend = scaling.forecast(self.series(values), 1e9, 0.20, min_samples=5)
        self.assertEqual("reset", trend.verdict)

    def test_a_zero_capacity_denominator_is_refused(self) -> None:
        trend = scaling.forecast(self.series([100.0 * i for i in range(1, 21)]), 0.0, 0.20, min_samples=5)
        self.assertEqual("no_data", trend.verdict)

    def test_a_negative_capacity_denominator_is_refused(self) -> None:
        trend = scaling.forecast(self.series([100.0 * i for i in range(1, 21)]), -1.0, 0.20, min_samples=5)
        self.assertEqual("no_data", trend.verdict)

    def test_a_rising_series_forecasts(self) -> None:
        # 1 Mbit/s per second, capacity 1 Gbit/s, RED at 20% free means the
        # threshold is 800 Mbit/s.
        values = [1e6 * i for i in range(1, 21)]
        trend = scaling.forecast(self.series(values, step=1.0), 1e9, 0.20, min_samples=5)
        self.assertEqual("ok", trend.verdict)
        self.assertTrue(trend.usable)
        self.assertAlmostEqual(1e6, trend.slope_bps_per_second, delta=1.0)
        self.assertAlmostEqual((800e6 - 20e6) / 1e6, trend.seconds_to_threshold, delta=1.0)

    def test_already_past_the_threshold_is_zero_not_negative(self) -> None:
        values = [900e6 + 1e6 * i for i in range(1, 21)]
        trend = scaling.forecast(self.series(values, step=1.0), 1e9, 0.20, min_samples=5)
        self.assertEqual("ok", trend.verdict)
        self.assertEqual(0.0, trend.seconds_to_threshold)

    def test_the_verdict_is_never_a_bare_number(self) -> None:
        # Every refusal has a name, so a dashboard can say why there is no date.
        random.seed(7)
        for _ in range(20):
            values = [random.uniform(-10, 10) for _ in range(8)]
            trend = scaling.forecast(self.series(values), 1e9, 0.20, min_samples=5)
            self.assertIn(
                trend.verdict, {"ok", "flat", "falling", "reset", "insufficient_history", "no_data"}
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
