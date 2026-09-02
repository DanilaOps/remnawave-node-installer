"""What the Semaphore duration exporter may read, and what it must compute.

Two separate concerns are tested here.

The first is arithmetic: a p95 over three runs has to be a p95 over three runs
and has to be labelled with the sample count, because "the install takes 9
minutes at p95" means something different when it is nine samples and when it
is one.

The second is restraint. A Semaphore task's output is a playbook log. A log is
where an inventory address, a survey secret or a mistyped credential ends up,
so this exporter must have no way to fetch one. That is asserted twice: once
against the client, which refuses the endpoint before it builds a request, and
once against the source file, so that a future edit that adds such a call fails
the build rather than being noticed later.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import semaphore_task_exporter as exporter  # noqa: E402

SOURCE = ROOT / "semaphore_task_exporter.py"


def task(template_id: int, status: str, start: str, end: str) -> dict:
    return {"template_id": template_id, "status": status, "start": start, "end": end}


class QuantileTests(unittest.TestCase):
    def test_no_samples_is_none_rather_than_zero(self) -> None:
        # Zero would render as "this job takes no time", which is worse than
        # publishing nothing.
        self.assertIsNone(exporter.quantile([], 0.5))

    def test_a_single_sample_is_that_sample(self) -> None:
        self.assertEqual(42.0, exporter.quantile([42], 0.95))

    def test_the_median_of_an_odd_count(self) -> None:
        self.assertEqual(3.0, exporter.quantile([1, 3, 9], 0.5))

    def test_the_median_of_an_even_count_interpolates(self) -> None:
        self.assertEqual(2.0, exporter.quantile([1, 3], 0.5))

    def test_p95_matches_linear_interpolation(self) -> None:
        samples = list(range(1, 101))
        self.assertAlmostEqual(95.05, exporter.quantile(samples, 0.95))


class TimestampTests(unittest.TestCase):
    def test_iso_with_z(self) -> None:
        self.assertEqual(
            exporter.parse_timestamp("2026-08-31T10:00:00Z"),
            exporter.parse_timestamp("2026-08-31T10:00:00+00:00"),
        )

    def test_extra_subsecond_digits_are_tolerated(self) -> None:
        self.assertIsNotNone(exporter.parse_timestamp("2026-08-31T10:00:00.123456789Z"))

    def test_nulls_are_not_timestamps(self) -> None:
        for value in (None, "", "null"):
            self.assertIsNone(exporter.parse_timestamp(value))


class SummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.templates = [{"id": 2, "name": "02 - Install / Reconcile Node"}]
        self.tasks = [
            task(2, "success", "2026-08-01T10:00:00Z", "2026-08-01T10:05:00Z"),   # 300s
            task(2, "success", "2026-08-02T10:00:00Z", "2026-08-02T10:10:00Z"),   # 600s
            task(2, "success", "2026-08-03T10:00:00Z", "2026-08-03T10:15:00Z"),   # 900s
            task(2, "error", "2026-08-04T10:00:00Z", "2026-08-04T10:00:20Z"),     # ignored
            task(2, "running", "2026-08-05T10:00:00Z", None),                     # ignored
        ]

    def test_only_successful_runs_are_summarised(self) -> None:
        # A failure's duration is how long it took to fail, which would drag the
        # percentiles around for reasons unrelated to how long the work takes.
        summary = exporter.summarise(self.templates, self.tasks)[2]
        self.assertEqual(3, len(summary["durations"]))
        self.assertEqual(600.0, exporter.quantile(summary["durations"], 0.5))

    def test_the_last_success_is_the_latest_one_not_the_last_listed(self) -> None:
        shuffled = [self.tasks[2], self.tasks[0], self.tasks[1]]
        summary = exporter.summarise(self.templates, shuffled)[2]
        self.assertEqual(
            exporter.parse_timestamp("2026-08-03T10:15:00Z"), summary["last_success"]
        )
        self.assertEqual(900.0, summary["last_success_duration"])

    def test_a_task_that_ends_before_it_starts_is_dropped(self) -> None:
        broken = [task(2, "success", "2026-08-01T10:05:00Z", "2026-08-01T10:00:00Z")]
        self.assertEqual([], exporter.summarise(self.templates, broken)[2]["durations"])

    def test_the_window_drops_ancient_runs(self) -> None:
        now = exporter.parse_timestamp("2026-08-31T10:00:00Z")
        summary = exporter.summarise(self.templates, self.tasks, window_seconds=7 * 86400, now=now)[2]
        self.assertEqual(0, len(summary["durations"]))

    def test_a_template_with_no_runs_still_reports_a_sample_count(self) -> None:
        summary = exporter.summarise(self.templates, [])
        self.assertEqual(0, len(summary[2]["durations"]))
        self.assertIsNone(summary[2]["last_success"])


class ExpositionTests(unittest.TestCase):
    class _Client:
        def templates(self, project: int) -> list[dict]:
            return [{"id": 2, "name": "02 - Install / Reconcile Node"}]

        def tasks(self, project: int, limit: int):
            # (tasks, pages_read, truncated) - the coverage metadata the
            # collector publishes beside the numbers.
            return (
                [
                    task(2, "success", "2026-08-01T10:00:00Z", "2026-08-01T10:05:00Z"),
                    task(2, "success", "2026-08-02T10:00:00Z", "2026-08-02T10:10:00Z"),
                ],
                1,
                False,
            )

    def setUp(self) -> None:
        self.text = exporter.Collector(self._Client(), [1], 100, None).collect()

    def test_it_publishes_the_four_things_that_were_asked_for(self) -> None:
        self.assertIn("august_semaphore_last_success_timestamp_seconds", self.text)
        self.assertIn("august_semaphore_last_success_duration_seconds", self.text)
        self.assertIn('august_semaphore_task_duration_seconds{project="1"', self.text)
        self.assertIn('quantile="0.5"', self.text)
        self.assertIn('quantile="0.95"', self.text)
        self.assertIn("august_semaphore_task_duration_samples", self.text)

    def test_the_sample_count_is_published_beside_the_percentiles(self) -> None:
        line = [ln for ln in self.text.splitlines() if ln.startswith("august_semaphore_task_duration_samples")]
        self.assertEqual(1, len(line))
        self.assertTrue(line[0].endswith(" 2"))

    def test_every_family_is_grouped(self) -> None:
        seen: list[str] = []
        for raw in self.text.splitlines():
            if raw.startswith("# HELP "):
                seen.append(raw.split()[2])
            elif raw and not raw.startswith("#"):
                self.assertEqual(seen[-1], raw.split("{")[0].split(" ")[0])


class RestraintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = exporter.SemaphoreClient("http://127.0.0.1:3000", "token")

    def test_task_output_is_refused_before_a_request_is_built(self) -> None:
        with self.assertRaises(exporter.ForbiddenEndpoint):
            self.client._get("/api/project/1/tasks/7/output")

    def test_other_project_data_is_refused(self) -> None:
        for path in (
            "/api/project/1/keys",
            "/api/project/1/environment",
            "/api/project/1/inventory",
            "/api/user/tokens",
        ):
            with self.subTest(path=path):
                with self.assertRaises(exporter.ForbiddenEndpoint):
                    self.client._get(path)

    def test_the_two_endpoints_it_needs_are_allowed(self) -> None:
        # Allowed means "gets as far as opening a socket", which is why this
        # asserts on the failure to connect rather than on a refusal.
        for path in ("/api/project/1/templates", "/api/project/1/tasks"):
            with self.subTest(path=path):
                try:
                    self.client._get(path)
                except exporter.ForbiddenEndpoint:  # pragma: no cover
                    self.fail(f"{path} must be readable")
                except Exception:
                    pass

    def test_the_source_contains_no_output_endpoint(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")
        offending = [
            line
            for line in source.splitlines()
            if "/output" in line and "FORBIDDEN" not in line and not line.strip().startswith("#")
        ]
        self.assertEqual([], offending, "a task-output endpoint appeared in the exporter")

    def test_the_token_is_read_from_the_environment_not_a_command_line(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn('os.environ.get("SEMAPHORE_API_TOKEN"', source)
        self.assertNotIn("--token", source)


class EnvelopeTests(unittest.TestCase):
    """Which answer shapes are understood, and what happens to the rest."""

    def setUp(self) -> None:
        self.client = exporter.SemaphoreClient("http://127.0.0.1:3000", "token")

    def test_a_bare_list(self) -> None:
        self.assertEqual([{"id": 1}], self.client._unwrap([{"id": 1}], "templates"))

    def test_each_known_envelope(self) -> None:
        for key in exporter.KNOWN_ENVELOPE_KEYS:
            with self.subTest(key=key):
                self.assertEqual([{"id": 1}], self.client._unwrap({key: [{"id": 1}]}, "templates"))

    def test_an_unknown_envelope_is_a_contract_error(self) -> None:
        # An upgrade that renames the wrapper must not read as an empty history.
        with self.assertRaises(exporter.ContractError):
            self.client._unwrap({"payload": [{"id": 1}]}, "templates")

    def test_two_candidate_envelopes_are_ambiguous(self) -> None:
        with self.assertRaises(exporter.ContractError):
            self.client._unwrap({"data": [], "tasks": []}, "tasks")

    def test_a_scalar_answer_is_a_contract_error(self) -> None:
        for payload in ("nope", 7, None):
            with self.subTest(payload=payload):
                with self.assertRaises(exporter.ContractError):
                    self.client._unwrap(payload, "tasks")

    def test_a_list_of_scalars_is_a_contract_error(self) -> None:
        with self.assertRaises(exporter.ContractError):
            self.client._unwrap([1, 2, 3], "tasks")


class PaginationTests(unittest.TestCase):
    """The task list is followed across pages, and cannot loop forever."""

    class PagedClient(exporter.SemaphoreClient):
        def __init__(self, pages: list[list[dict]], envelope: str | None = None) -> None:
            super().__init__("http://127.0.0.1:3000", "token")
            self.pages = pages
            self.envelope = envelope
            self.calls: list[dict] = []

        def _get(self, path: str, query: dict | None = None):  # type: ignore[override]
            self.calls.append({"path": path, "query": dict(query or {})})
            if path.endswith("/templates"):
                return [{"id": 2, "name": "02 - Install / Reconcile Node"}]
            offset = int((query or {}).get("offset", 0))
            limit = int((query or {}).get("limit", 100))
            flat = [item for page in self.pages for item in page]
            window = flat[offset : offset + limit]
            return {self.envelope: window} if self.envelope else window

    @staticmethod
    def page(start: int, count: int) -> list[dict]:
        return [
            {
                "id": start + index,
                "template_id": 2,
                "status": "success",
                "start": "2026-08-01T10:00:00Z",
                "end": "2026-08-01T10:05:00Z",
            }
            for index in range(count)
        ]

    def test_pages_are_followed_until_the_history_ends(self) -> None:
        client = self.PagedClient([self.page(1, 250)])
        tasks, pages, truncated = client.tasks(1, limit=500, page_size=100)
        self.assertEqual(250, len(tasks))
        self.assertEqual(3, pages)
        self.assertFalse(truncated)

    def test_the_limit_is_respected_and_reported_as_truncated(self) -> None:
        client = self.PagedClient([self.page(1, 500)])
        tasks, _pages, truncated = client.tasks(1, limit=120, page_size=100)
        self.assertEqual(120, len(tasks))
        self.assertTrue(truncated, "a percentile over a truncated window has to say so")

    def test_the_page_cap_stops_a_runaway(self) -> None:
        client = self.PagedClient([self.page(1, 10_000)])
        tasks, pages, truncated = client.tasks(1, limit=10_000, page_size=10, max_pages=5)
        self.assertEqual(50, len(tasks))
        self.assertEqual(5, pages)
        self.assertTrue(truncated)

    def test_an_api_that_ignores_offset_does_not_loop_forever(self) -> None:
        class StuckClient(self.PagedClient):
            def _get(self, path, query=None):  # type: ignore[override]
                if path.endswith("/templates"):
                    return []
                # Always the same first page, whatever offset was asked for.
                return PaginationTests.page(1, 10)

        client = StuckClient([])
        tasks, pages, _truncated = client.tasks(1, limit=1000, page_size=10)
        self.assertEqual(10, len(tasks), "duplicate ids must be dropped, not collected forever")
        self.assertLessEqual(pages, 2)

    def test_an_envelope_is_followed_too(self) -> None:
        client = self.PagedClient([self.page(1, 150)], envelope="data")
        tasks, _pages, _truncated = client.tasks(1, limit=500, page_size=100)
        self.assertEqual(150, len(tasks))

    def test_a_task_without_the_required_fields_is_a_contract_error(self) -> None:
        client = self.PagedClient([[{"id": 1, "status": "success"}]])
        with self.assertRaises(exporter.ContractError):
            client.tasks(1, limit=10)

    def test_offset_and_limit_are_actually_sent(self) -> None:
        client = self.PagedClient([self.page(1, 150)])
        client.tasks(1, limit=150, page_size=100)
        task_calls = [call for call in client.calls if call["path"].endswith("/tasks")]
        self.assertEqual({"limit": 100, "offset": 0}, task_calls[0]["query"])
        self.assertEqual({"limit": 50, "offset": 100}, task_calls[1]["query"])


class StatusAndCoverageTests(unittest.TestCase):
    """Failed and running tasks are counted, and never turned into durations."""

    class MixedClient:
        def templates(self, project: int) -> list[dict]:
            return [{"id": 2, "name": "02 - Install / Reconcile Node"}]

        def tasks(self, project: int, limit: int) -> tuple[list[dict], int, bool]:
            return (
                [
                    task(2, "success", "2026-08-01T10:00:00Z", "2026-08-01T10:05:00Z"),
                    task(2, "error", "2026-08-02T10:00:00Z", "2026-08-02T10:00:10Z"),
                    task(2, "running", "2026-08-03T10:00:00Z", None),
                    task(2, "stopped", "2026-08-04T10:00:00Z", "2026-08-04T10:00:30Z"),
                    task(2, "brand_new_state", "2026-08-05T10:00:00Z", "2026-08-05T10:00:30Z"),
                ],
                1,
                False,
            )

    def setUp(self) -> None:
        self.text = exporter.Collector(self.MixedClient(), [1], 100, None).collect()

    def test_every_status_is_counted(self) -> None:
        for status, count in (("success", 1), ("error", 1), ("running", 1), ("stopped", 1), ("unknown", 1)):
            with self.subTest(status=status):
                self.assertIn(f'status="{status}"', self.text)
                self.assertRegex(self.text, rf'august_semaphore_tasks\{{[^}}]*status="{status}"[^}}]*}} {count}')

    def test_only_the_successful_run_becomes_a_duration(self) -> None:
        line = [ln for ln in self.text.splitlines() if ln.startswith("august_semaphore_task_duration_samples")]
        self.assertEqual(1, len(line))
        self.assertTrue(line[0].endswith(" 1"))

    def test_coverage_metadata_is_published(self) -> None:
        for metric in (
            "august_semaphore_tasks_read",
            "august_semaphore_pages_read",
            "august_semaphore_history_truncated",
            "august_semaphore_window_seconds",
            "august_semaphore_oldest_task_timestamp_seconds",
        ):
            with self.subTest(metric=metric):
                self.assertIn(metric, self.text)

    def test_the_contract_is_reported_as_ok(self) -> None:
        self.assertIn('august_semaphore_response_contract_ok{project="1"} 1', self.text)


class ContractFailureTests(unittest.TestCase):
    class BrokenClient:
        def templates(self, project: int) -> list[dict]:
            raise exporter.ContractError("templates came back as a mapping of mappings")

        def tasks(self, project: int, limit: int):
            raise exporter.ContractError("unreachable")

    class UnreachableClient:
        def templates(self, project: int) -> list[dict]:
            raise OSError("connection refused")

        def tasks(self, project: int, limit: int):
            raise OSError("connection refused")

    def test_an_unknown_shape_sets_scrape_ok_to_zero(self) -> None:
        text = exporter.Collector(self.BrokenClient(), [1], 100, None).collect()
        self.assertIn('august_semaphore_scrape_ok{project="1"} 0', text)
        self.assertIn('august_semaphore_response_contract_ok{project="1"} 0', text)
        # And no numbers at all, rather than zeroes that read as "nothing ran".
        self.assertNotIn("august_semaphore_task_duration_seconds{", text)

    def test_unreachable_is_distinguished_from_a_bad_shape(self) -> None:
        text = exporter.Collector(self.UnreachableClient(), [1], 100, None).collect()
        self.assertIn('august_semaphore_scrape_ok{project="1"} 0', text)
        self.assertIn('august_semaphore_response_contract_ok{project="1"} 1', text)


class EmptyHistoryTests(unittest.TestCase):
    class EmptyClient:
        def templates(self, project: int) -> list[dict]:
            return [{"id": 2, "name": "02 - Install / Reconcile Node"}]

        def tasks(self, project: int, limit: int):
            return [], 0, False

    def test_no_history_publishes_a_zero_sample_count_and_no_percentiles(self) -> None:
        text = exporter.Collector(self.EmptyClient(), [1], 100, None).collect()
        self.assertIn('august_semaphore_scrape_ok{project="1"} 1', text)
        self.assertRegex(text, r"august_semaphore_task_duration_samples\{[^}]*\} 0")
        self.assertNotIn("august_semaphore_task_duration_seconds{", text)
        self.assertNotIn("august_semaphore_last_success_timestamp_seconds{", text)


class TimezoneTests(unittest.TestCase):
    def test_a_naive_timestamp_is_read_as_utc(self) -> None:
        self.assertEqual(
            exporter.parse_timestamp("2026-08-31T10:00:00"),
            exporter.parse_timestamp("2026-08-31T10:00:00Z"),
        )

    def test_an_offset_is_honoured(self) -> None:
        east = exporter.parse_timestamp("2026-08-31T12:00:00+02:00")
        utc = exporter.parse_timestamp("2026-08-31T10:00:00Z")
        self.assertEqual(utc, east)

    def test_a_duration_across_a_timezone_boundary_is_right(self) -> None:
        summary = exporter.summarise(
            [{"id": 2, "name": "t"}],
            [task(2, "success", "2026-08-31T12:00:00+02:00", "2026-08-31T10:05:00Z")],
        )
        self.assertEqual([300.0], summary[2]["durations"])

    def test_epoch_milliseconds_are_recognised(self) -> None:
        self.assertAlmostEqual(1788000000.0, exporter.parse_timestamp(1788000000000), delta=1.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
