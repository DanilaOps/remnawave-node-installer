"""The node-side collection loop, tested without a server.

Public iperf3 endpoints fail in mundane ways - busy, down, slow, on the wrong
port - and none of them may take the whole test with them. These tests inject a
fake iperf and a fake probe so every one of those failures is reproducible and
the collection's response to it is pinned.
"""

from __future__ import annotations

import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import iperf_runner as runner  # noqa: E402


def ok_body(mbps: float) -> str:
    return json.dumps({"end": {"sum_sent": {"bits_per_second": mbps * 1e6 * 1.05},
                               "sum_received": {"bits_per_second": mbps * 1e6}}})


BUSY = json.dumps({"error": "the server is busy running a test. try again later"})


def config(endpoints, **over):
    base = {"runs": 3, "duration_seconds": 1, "streams": 4, "retries": 1,
            "connect_timeout_seconds": 1, "run_timeout_seconds": 2, "endpoints": endpoints}
    base.update(over)
    return base


class TargetSelectionTests(unittest.TestCase):
    def test_the_first_reachable_port_is_chosen(self) -> None:
        probed = []

        def probe(host, port, timeout):
            probed.append((host, port))
            return port == 5203  # only the third port answers

        target = runner._find_target([{"host": "h", "ports": [5201, 5202, 5203, 5204]}], probe, 1)
        self.assertEqual(("h", 5203), target)
        # It stopped at the first open port.
        self.assertEqual([("h", 5201), ("h", 5202), ("h", 5203)], probed)

    def test_the_fallback_host_is_used_when_the_primary_is_dead(self) -> None:
        def probe(host, port, timeout):
            return host == "fallback"

        target = runner._find_target(
            [{"host": "primary", "ports": [5201, 5202]}, {"host": "fallback", "ports": [5201]}],
            probe, 1)
        self.assertEqual(("fallback", 5201), target)

    def test_no_reachable_target_is_none(self) -> None:
        self.assertIsNone(runner._find_target([{"host": "h", "ports": [5201]}], lambda *a: False, 1))


class CollectTests(unittest.TestCase):
    def _iperf_fixed(self, mapping):
        """A fake iperf: mapping[(host, reverse)] -> body, default busy."""
        calls = []

        def iperf(host, port, streams, duration, reverse, timeout):
            calls.append({"host": host, "port": port, "reverse": reverse})
            return mapping.get((host, reverse), BUSY)

        return iperf, calls

    def test_download_uses_reverse_and_upload_does_not(self) -> None:
        iperf, calls = self._iperf_fixed({("h", True): ok_body(8000), ("h", False): ok_body(7000)})
        result = runner.collect(
            config([{"city": "Moscow", "hosts": [{"host": "h", "ports": [5201]}]}]),
            iperf=iperf, probe=lambda *a: True,
        )
        self.assertEqual([8000, 8000, 8000], result["Moscow"]["download"])
        self.assertEqual([7000, 7000, 7000], result["Moscow"]["upload"])
        # download runs asked for reverse=True, upload for reverse=False.
        self.assertTrue(all(c["reverse"] for c in calls if c["reverse"]))
        self.assertIn(True, [c["reverse"] for c in calls])
        self.assertIn(False, [c["reverse"] for c in calls])

    def test_a_dead_city_is_absent_from_the_result(self) -> None:
        iperf, _ = self._iperf_fixed({("live", True): ok_body(6000), ("live", False): ok_body(5000)})

        def probe(host, port, timeout):
            return host == "live"

        result = runner.collect(
            config([
                {"city": "Moscow", "hosts": [{"host": "live", "ports": [5201]}]},
                {"city": "Dead", "hosts": [{"host": "dead", "ports": [5201]}]},
            ]),
            iperf=iperf, probe=probe,
        )
        self.assertIn("Moscow", result)
        self.assertNotIn("Dead", result)

    def test_a_busy_server_yields_no_runs_and_the_city_is_dropped(self) -> None:
        # Reachable (probe true) but every iperf run is the busy error.
        def iperf(host, port, streams, duration, reverse, timeout):
            return BUSY

        result = runner.collect(
            config([{"city": "Busy", "hosts": [{"host": "h", "ports": [5201]}]}]),
            iperf=iperf, probe=lambda *a: True,
        )
        self.assertNotIn("Busy", result)

    def test_a_timeout_is_an_empty_body_and_is_skipped(self) -> None:
        def iperf(host, port, streams, duration, reverse, timeout):
            return ""  # what default_iperf returns on TimeoutExpired

        result = runner.collect(
            config([{"city": "Slow", "hosts": [{"host": "h", "ports": [5201]}]}]),
            iperf=iperf, probe=lambda *a: True,
        )
        self.assertNotIn("Slow", result)

    def test_retries_top_up_to_the_wanted_run_count(self) -> None:
        # Fail the first attempt, then succeed: with retries=1 and runs=3, four
        # attempts are allowed, so three good runs are still collected.
        state = {"n": 0}

        def iperf(host, port, streams, duration, reverse, timeout):
            state["n"] += 1
            return BUSY if state["n"] == 1 else ok_body(6000)

        result = runner.collect(
            config([{"city": "Moscow", "hosts": [{"host": "h", "ports": [5201]}]}], runs=3, retries=1),
            iperf=iperf, probe=lambda *a: True,
        )
        # download is measured first; its first attempt was busy, next three good.
        self.assertEqual([6000, 6000, 6000], result["Moscow"]["download"])

    def test_a_wedged_server_cannot_exceed_the_attempt_budget(self) -> None:
        calls = {"n": 0}

        def iperf(host, port, streams, duration, reverse, timeout):
            calls["n"] += 1
            return BUSY

        runner.collect(
            config([{"city": "X", "hosts": [{"host": "h", "ports": [5201]}]}], runs=3, retries=1),
            iperf=iperf, probe=lambda *a: True,
        )
        # runs+retries = 4 attempts per direction, two directions = 8, no more.
        self.assertLessEqual(calls["n"], 8)

    def test_a_partially_working_city_keeps_the_direction_that_worked(self) -> None:
        iperf, _ = self._iperf_fixed({("h", True): ok_body(8000)})  # only download works
        result = runner.collect(
            config([{"city": "Moscow", "hosts": [{"host": "h", "ports": [5201]}]}]),
            iperf=iperf, probe=lambda *a: True,
        )
        self.assertEqual([8000, 8000, 8000], result["Moscow"]["download"])
        self.assertEqual([], result["Moscow"]["upload"])


class EndToEndShapeTests(unittest.TestCase):
    def test_collect_feeds_analyze_to_a_certain_rating(self) -> None:
        import iperf_capacity as ic

        def iperf(host, port, streams, duration, reverse, timeout):
            rate = {"msk": 6400, "chel": 8100}[host] + (0 if reverse else -400)
            return ok_body(rate)

        measurements = runner.collect(
            config([
                {"city": "Moscow", "hosts": [{"host": "msk", "ports": [5201]}]},
                {"city": "Chelyabinsk", "hosts": [{"host": "chel", "ports": [5201]}]},
            ]),
            iperf=iperf, probe=lambda *a: True,
        )
        result = ic.analyze(measurements, safety_factor=0.90, round_down_mbps=100, min_valid_cities=2)
        self.assertTrue(result["certain"])
        self.assertEqual(7200, result["download_mbps"])  # best 8100 * 0.9 = 7290 -> 7200


if __name__ == "__main__":
    unittest.main()
