"""The auto-capacity arithmetic, proved against fixtures rather than a server.

Every rule the task pinned down is one test here: which iperf3 field to trust,
which direction a run measures, the median per city, the maximum across cities,
that a slow Tyumen cannot drag a fast Moscow down, the safety factor, the
always-down rounding, and the refusal to publish a figure too few cities
confirmed. No network is touched; the iperf3 JSON is canned.
"""

from __future__ import annotations

import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import iperf_capacity as ic  # noqa: E402


def iperf_json(sent_bps: float, received_bps: float) -> str:
    """A well-formed iperf3 --json body with distinct sent/received rates, so a
    test can tell which field the parser reads."""
    return json.dumps({
        "start": {"connected": [{"remote_host": "203.0.113.1"}]},
        "end": {
            "sum_sent": {"bits_per_second": sent_bps, "retransmits": 12},
            "sum_received": {"bits_per_second": received_bps},
        },
    })


class DirectionAndParseTests(unittest.TestCase):
    def test_the_receiving_end_is_read_not_the_sending_end(self) -> None:
        # sum_sent is what a sender pushed; sum_received is what arrived. Capacity
        # is what arrived.
        body = iperf_json(sent_bps=7_000_000_000, received_bps=6_500_000_000)
        self.assertEqual(6_500_000_000.0, ic.parse_bits_per_second(body))
        self.assertEqual(6500.0, ic.mbps(body))

    def test_direction_is_the_flag_we_passed_not_a_json_label(self) -> None:
        # A normal run measures the node's upload; -R measures its download.
        self.assertEqual("upload", ic.NORMAL_DIRECTION)
        self.assertEqual("download", ic.REVERSE_DIRECTION)

    def test_an_iperf_error_object_is_not_a_measurement(self) -> None:
        self.assertIsNone(ic.parse_bits_per_second({"error": "unable to connect to server: Connection refused"}))
        self.assertIsNone(ic.mbps('{"error": "the server is busy running a test. try again later"}'))

    def test_invalid_json_is_ignored_safely(self) -> None:
        self.assertIsNone(ic.parse_bits_per_second("not json at all"))
        self.assertIsNone(ic.parse_bits_per_second("{truncated"))

    def test_a_missing_or_nonpositive_rate_is_ignored(self) -> None:
        self.assertIsNone(ic.parse_bits_per_second({"end": {"sum_received": {}}}))
        self.assertIsNone(ic.parse_bits_per_second({"end": {}}))
        self.assertIsNone(ic.parse_bits_per_second({"end": {"sum_received": {"bits_per_second": 0}}}))
        self.assertIsNone(ic.parse_bits_per_second({"end": {"sum_received": {"bits_per_second": -5}}}))
        self.assertIsNone(ic.parse_bits_per_second({"end": {"sum_received": {"bits_per_second": True}}}))


class CityMedianTests(unittest.TestCase):
    def test_three_runs_take_the_median_not_the_mean_or_the_peak(self) -> None:
        self.assertEqual(6200, ic.city_median([6100, 6350, 6200]))

    def test_an_even_number_of_runs_averages_the_middle_two(self) -> None:
        self.assertEqual(6250, ic.city_median([6100, 6400, 6200, 6300]))

    def test_a_single_run_is_its_own_median(self) -> None:
        self.assertEqual(2300, ic.city_median([2300]))

    def test_a_city_with_no_successful_run_has_no_median(self) -> None:
        self.assertIsNone(ic.city_median([]))
        self.assertIsNone(ic.city_median([0, -1]))


class AnalyzeTests(unittest.TestCase):
    FIVE_CITIES = {
        "Moscow": {"download": [6300, 6400, 6350], "upload": [6100, 6000, 6050]},
        "SPB": {"download": [5900, 5950, 5850], "upload": [5700, 5800, 5750]},
        "NizhnyNovgorod": {"download": [6300, 6250, 6350], "upload": [6100, 6150, 6050]},
        "Chelyabinsk": {"download": [8100, 8155, 8050], "upload": [7900, 7950, 7850]},
        "Tyumen": {"download": [2300, 2250, 2350], "upload": [2200, 2100, 2300]},
    }

    def test_the_maximum_city_median_wins_and_tyumen_does_not_drag_it_down(self) -> None:
        result = ic.analyze(self.FIVE_CITIES, safety_factor=0.90, round_down_mbps=100, min_valid_cities=2)
        # Chelyabinsk download median 8100 is the best; Tyumen's 2300 is ignored.
        self.assertEqual(8100, result["best"]["download"])
        # 8100 * 0.90 = 7290 -> round down to 7200.
        self.assertEqual(7200, result["download_mbps"])
        self.assertTrue(result["certain"])

    def test_download_and_upload_are_rated_independently(self) -> None:
        result = ic.analyze(self.FIVE_CITIES, safety_factor=0.90, round_down_mbps=100, min_valid_cities=2)
        # upload best is Chelyabinsk 7900 -> *0.9 = 7110 -> 7100.
        self.assertEqual(7900, result["best"]["upload"])
        self.assertEqual(7100, result["upload_mbps"])
        self.assertNotEqual(result["download_mbps"], result["upload_mbps"])

    def test_the_safety_factor_is_applied(self) -> None:
        one = {"A": {"download": [10000], "upload": [10000]}, "B": {"download": [9000], "upload": [9000]}}
        result = ic.analyze(one, safety_factor=0.80, round_down_mbps=100, min_valid_cities=2)
        # best 10000 * 0.80 = 8000.
        self.assertEqual(8000, result["download_mbps"])

    def test_rounding_is_always_down(self) -> None:
        cities = {"A": {"download": [8155], "upload": [7900]}, "B": {"download": [8000], "upload": [7800]}}
        result = ic.analyze(cities, safety_factor=0.90, round_down_mbps=100, min_valid_cities=2)
        # 8155 * 0.90 = 7339.5 -> 7300 (down, not 7400).
        self.assertEqual(7300, result["download_mbps"])
        # 7900 * 0.90 = 7110 -> 7100.
        self.assertEqual(7100, result["upload_mbps"])

    def test_a_dead_city_is_ignored_not_counted_as_zero(self) -> None:
        cities = {
            "Moscow": {"download": [6400], "upload": [6000]},
            "SPB": {"download": [6300], "upload": [5900]},
            "Dead": {"download": [], "upload": []},
        }
        result = ic.analyze(cities, safety_factor=0.90, round_down_mbps=100, min_valid_cities=2)
        self.assertEqual(2, result["valid_cities"]["download"])
        self.assertIsNone(result["cities"]["Dead"]["download"])
        self.assertEqual(6400, result["best"]["download"])
        self.assertTrue(result["certain"])

    def test_too_few_valid_cities_is_uncertain_and_unrated(self) -> None:
        # Only one city with a download result.
        cities = {
            "Moscow": {"download": [6400], "upload": [6000]},
            "SPB": {"download": [], "upload": [5900]},
        }
        result = ic.analyze(cities, safety_factor=0.90, round_down_mbps=100, min_valid_cities=2)
        self.assertEqual(1, result["valid_cities"]["download"])
        self.assertFalse(result["certain"])
        self.assertIsNone(result["download_mbps"])
        self.assertIsNone(result["upload_mbps"])

    def test_one_direction_short_makes_the_whole_node_unmeasured(self) -> None:
        # Download has two cities, upload only one -> not certain, both unrated.
        cities = {
            "Moscow": {"download": [6400], "upload": [6000]},
            "SPB": {"download": [6300], "upload": []},
        }
        result = ic.analyze(cities, safety_factor=0.90, round_down_mbps=100, min_valid_cities=2)
        self.assertEqual(2, result["valid_cities"]["download"])
        self.assertEqual(1, result["valid_cities"]["upload"])
        self.assertFalse(result["certain"])
        self.assertIsNone(result["download_mbps"])

    def test_empty_input_is_uncertain(self) -> None:
        result = ic.analyze({}, min_valid_cities=2)
        self.assertFalse(result["certain"])
        self.assertIsNone(result["download_mbps"])

    def test_a_worked_example_matches_the_specification(self) -> None:
        # The numbers from the task description.
        cities = {
            "Moscow": {"download": [6400], "upload": [6900]},
            "SPB": {"download": [5900], "upload": [6800]},
            "NN": {"download": [6300], "upload": [6850]},
            "Chelyabinsk": {"download": [8155], "upload": [7900]},
            "Tyumen": {"download": [2300], "upload": [2200]},
        }
        result = ic.analyze(cities, safety_factor=0.90, round_down_mbps=100, min_valid_cities=2)
        self.assertEqual(7300, result["download_mbps"])   # 8155*0.9=7339.5 -> 7300
        self.assertEqual(7100, result["upload_mbps"])     # 7900*0.9=7110 -> 7100
        self.assertTrue(result["certain"])


class CliAndSummaryTests(unittest.TestCase):
    def test_cli_emits_result_json(self) -> None:
        import io
        import tempfile
        cities = {"A": {"download": [8000], "upload": [7000]}, "B": {"download": [7500], "upload": [6500]}}
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "m.json"
            path.write_text(json.dumps(cities), encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            old = sys.stdout, sys.stderr
            sys.stdout, sys.stderr = out, err
            try:
                rc = ic.main(["--measurements", str(path), "--node", "TR-02"])
            finally:
                sys.stdout, sys.stderr = old
            self.assertEqual(0, rc)
            result = json.loads(out.getvalue())
            self.assertEqual(7200, result["download_mbps"])  # 8000*0.9=7200
            self.assertIn("TR-02", err.getvalue())

    def test_summary_names_every_city_and_the_verdict(self) -> None:
        result = ic.analyze(AnalyzeTests.FIVE_CITIES, min_valid_cities=2)
        text = ic.summary_text("TR-02", result)
        self.assertIn("Tyumen", text)
        self.assertIn("Chelyabinsk", text)
        self.assertIn("capacity_certain=true", text)


if __name__ == "__main__":
    unittest.main()
