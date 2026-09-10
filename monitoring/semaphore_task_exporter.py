#!/usr/bin/env python3
"""Prometheus exporter for how long Semaphore jobs take.

It answers one operational question - how long does installing a node take,
and is that getting worse - from the task list alone:

  * when the last successful run of a template finished, and how long it took;
  * the p50 and p95 of recent successful runs;
  * how many samples those percentiles were computed from, so that a p95 over
    three runs is visibly a p95 over three runs.

What it deliberately does not do: it never fetches task output. A task's output
is a playbook log, and a playbook log is where a mistyped credential or an
inventory address would end up; this exporter has no code path that can reach
that endpoint, and monitoring/tests/test_semaphore_task_exporter.py fails the build
if one appears. It only ever issues GETs, and the API token arrives in the
environment from a 0600 file rather than on a command line.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Sequence

EXPORTER_VERSION = "2"
QUANTILES = (0.5, 0.95)

# Endpoints this exporter is allowed to call. Anything else - task output above
# all - is refused before the request is built rather than reviewed later.
ALLOWED_PATH_SUFFIXES = ("/templates", "/tasks")
FORBIDDEN_PATH_FRAGMENTS = ("output", "secret", "key", "environment", "inventory")


class ForbiddenEndpoint(RuntimeError):
    """Raised when something asks this exporter to read more than it may."""


class ContractError(RuntimeError):
    """The API answered in a shape this exporter does not understand.

    Raised rather than guessed at. A Semaphore upgrade that wraps the task list
    in an envelope, renames a field or paginates differently would otherwise
    turn into "no successful runs found", which reads as a fleet nobody has
    installed anything on. Unknown shape means scrape_ok=0 and no numbers.
    """


# What a task object has to look like before any arithmetic is done with it.
REQUIRED_TASK_FIELDS = ("template_id", "status")
# Envelope keys this exporter accepts around a list of items. Anything else is a
# contract error.
KNOWN_ENVELOPE_KEYS = ("data", "tasks", "templates", "items", "result")
# Statuses Semaphore uses. An unknown one is reported rather than ignored: it
# might be a new terminal state that should count towards durations.
KNOWN_STATUSES = ("success", "error", "stopped", "stopping", "running", "waiting", "starting")


def escape_label(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_sample(name: str, labels: dict[str, Any], value: float) -> str:
    pairs = ",".join(f'{key}="{escape_label(labels[key])}"' for key in sorted(labels) if labels[key] not in (None, ""))
    body = f"{{{pairs}}}" if pairs else ""
    return f"{name}{body} {value:g}"


def parse_timestamp(value: Any) -> float | None:
    if value in (None, "", "null"):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) / 1000.0 if value > 1e11 else float(value)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # Semaphore emits more sub-second digits than fromisoformat accepts on some
    # runtimes; trimming them changes nothing that matters at second resolution.
    if "." in text:
        head, _, tail = text.partition(".")
        digits = ""
        rest = ""
        for index, character in enumerate(tail):
            if character.isdigit() and index < 6:
                digits += character
            elif character.isdigit():
                continue
            else:
                rest = tail[index:]
                break
        text = f"{head}.{digits or '0'}{rest}"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.timestamp()


def quantile(samples: Sequence[float], fraction: float) -> float | None:
    """Linear interpolation between order statistics.

    The same definition numpy calls "linear" and Prometheus's own
    quantile_over_time uses, so a number here and a number in a PromQL panel do
    not disagree for the same data.
    """
    if not samples:
        return None
    ordered = sorted(samples)
    if len(ordered) == 1:
        return float(ordered[0])
    position = fraction * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return float(ordered[lower] * (1 - weight) + ordered[upper] * weight)


class SemaphoreClient:
    """Read-only access to one Semaphore project's task list."""

    def __init__(self, base_url: str, token: str, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _get(self, path: str, query: dict[str, Any] | None = None) -> Any:
        lowered = path.lower()
        for fragment in FORBIDDEN_PATH_FRAGMENTS:
            if fragment in lowered:
                raise ForbiddenEndpoint(f"this exporter must not read {path}")
        if not any(lowered.endswith(suffix) for suffix in ALLOWED_PATH_SUFFIXES):
            raise ForbiddenEndpoint(f"{path} is not one of the endpoints this exporter may read")
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        request = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _unwrap(payload: Any, what: str) -> list[dict[str, Any]]:
        """A bare list, or a list under one known envelope key. Nothing else."""
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            keys = [key for key in KNOWN_ENVELOPE_KEYS if isinstance(payload.get(key), list)]
            if len(keys) != 1:
                raise ContractError(
                    f"{what}: expected a list or one of {list(KNOWN_ENVELOPE_KEYS)}, "
                    f"got a mapping with keys {sorted(payload)[:8]}"
                )
            items = payload[keys[0]]
        else:
            raise ContractError(f"{what}: expected a list, got {type(payload).__name__}")
        for item in items:
            if not isinstance(item, dict):
                raise ContractError(f"{what}: expected objects in the list, got {type(item).__name__}")
        return items

    def templates(self, project: int) -> list[dict[str, Any]]:
        items = self._unwrap(self._get(f"/api/project/{int(project)}/templates"), "templates")
        for item in items:
            if "id" not in item:
                raise ContractError("templates: an entry has no id")
        return items

    def tasks(self, project: int, limit: int, page_size: int = 100, max_pages: int = 50) -> tuple[list[dict[str, Any]], int, bool]:
        """Recent tasks, following pages until limit is reached.

        Returns (tasks, pages_read, truncated). "truncated" means the page cap
        or the limit was hit before the history ran out, which is published as
        coverage metadata: a p95 over a truncated window is a p95 over that
        window and the dashboard has to be able to say so.
        """
        collected: list[dict[str, Any]] = []
        pages = 0
        truncated = False
        seen_ids: set[Any] = set()
        while len(collected) < limit and pages < max_pages:
            want = min(page_size, limit - len(collected))
            payload = self._get(
                f"/api/project/{int(project)}/tasks",
                {"limit": want, "offset": len(collected)},
            )
            page = self._unwrap(payload, "tasks")
            if not page:
                break
            fresh = 0
            for item in page:
                for field in REQUIRED_TASK_FIELDS:
                    if field not in item:
                        raise ContractError(f"tasks: an entry has no {field}")
                identifier = item.get("id", id(item))
                if identifier in seen_ids:
                    # An API that ignores offset would otherwise loop forever
                    # handing back the same first page.
                    continue
                seen_ids.add(identifier)
                collected.append(item)
                fresh += 1
            pages += 1
            if fresh == 0:
                break
            if len(page) < want:
                break
        else:
            truncated = True
        if len(collected) >= limit:
            truncated = True
        return collected, pages, truncated


def summarise(
    templates: Sequence[dict[str, Any]],
    tasks: Sequence[dict[str, Any]],
    window_seconds: float | None = None,
    now: float | None = None,
) -> dict[int, dict[str, Any]]:
    """Per template: last success, its duration, the percentiles and the count.

    Only successful runs are summarised. A failed run's duration says how long
    it took to fail, which is a different quantity and would drag a p95 around
    for reasons that have nothing to do with how long the work takes.
    """
    now = time.time() if now is None else now
    names = {int(item["id"]): str(item.get("name") or item["id"]) for item in templates if "id" in item}

    def empty(name: str) -> dict[str, Any]:
        return {
            "name": name,
            "durations": [],
            "last_success": None,
            "last_success_duration": None,
            # Every status seen, not only the successful ones: a template whose
            # last twenty runs all failed has no successful duration at all, and
            # "no data" and "always fails" must not look the same.
            "statuses": {},
            "oldest_end": None,
        }

    result: dict[int, dict[str, Any]] = {
        template_id: empty(name) for template_id, name in names.items()
    }
    for task in tasks:
        template_id = task.get("template_id")
        if template_id is None:
            continue
        template_id = int(template_id)
        bucket = result.setdefault(template_id, empty(names.get(template_id, str(template_id))))
        status = str(task.get("status", "")).lower() or "unknown"
        if status not in KNOWN_STATUSES:
            status = "unknown"
        bucket["statuses"][status] = bucket["statuses"].get(status, 0) + 1

        end = parse_timestamp(task.get("end"))
        if end is not None and (bucket["oldest_end"] is None or end < bucket["oldest_end"]):
            bucket["oldest_end"] = end

        if status != "success":
            # A failure's duration is how long it took to fail, and a running
            # task has no duration at all. Neither belongs in a percentile of
            # how long the work takes.
            continue
        start = parse_timestamp(task.get("start"))
        if start is None or end is None or end < start:
            continue
        if window_seconds is not None and (now - end) > window_seconds:
            continue
        duration = end - start
        bucket["durations"].append(duration)
        if bucket["last_success"] is None or end > bucket["last_success"]:
            bucket["last_success"] = end
            bucket["last_success_duration"] = duration
    return result


class Collector:
    def __init__(self, client: SemaphoreClient | None, projects: Sequence[int], limit: int, window_seconds: float | None) -> None:
        self.client = client
        self.projects = list(projects)
        self.limit = limit
        self.window_seconds = window_seconds

    def collect(self) -> str:
        started = time.time()
        lines: list[str] = [
            "# HELP august_semaphore_exporter_build_info Exporter build information.",
            "# TYPE august_semaphore_exporter_build_info gauge",
            render_sample("august_semaphore_exporter_build_info", {"version": EXPORTER_VERSION}, 1),
        ]
        ok_samples: list[str] = []
        contract_samples: list[str] = []
        last_success: list[str] = []
        last_duration: list[str] = []
        quantile_samples: list[str] = []
        count_samples: list[str] = []
        status_samples: list[str] = []
        coverage_samples: list[str] = []

        for project in self.projects:
            reachable = 0
            contract_ok = 1
            summary: dict[int, dict[str, Any]] = {}
            pages = 0
            truncated = False
            task_count = 0
            if self.client is not None:
                try:
                    templates = self.client.templates(project)
                    tasks, pages, truncated = self.client.tasks(project, self.limit)
                    task_count = len(tasks)
                    summary = summarise(templates, tasks, window_seconds=self.window_seconds)
                    reachable = 1
                except ContractError:
                    # The API answered and this exporter does not understand it.
                    # Publishing zeroes here would read as "nothing ever ran".
                    reachable = 0
                    contract_ok = 0
                except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, OSError, ForbiddenEndpoint):
                    reachable = 0
            ok_samples.append(render_sample("august_semaphore_scrape_ok", {"project": project}, reachable))
            contract_samples.append(
                render_sample("august_semaphore_response_contract_ok", {"project": project}, contract_ok)
            )
            coverage_samples.append(
                render_sample("august_semaphore_tasks_read", {"project": project}, task_count)
            )
            coverage_samples.append(
                render_sample("august_semaphore_pages_read", {"project": project}, pages)
            )
            coverage_samples.append(
                render_sample("august_semaphore_history_truncated", {"project": project}, 1 if truncated else 0)
            )
            coverage_samples.append(
                render_sample(
                    "august_semaphore_window_seconds",
                    {"project": project},
                    self.window_seconds if self.window_seconds is not None else 0,
                )
            )
            for template_id, bucket in sorted(summary.items()):
                labels = {"project": project, "template": bucket["name"], "template_id": template_id}
                if bucket["last_success"] is not None:
                    last_success.append(
                        render_sample("august_semaphore_last_success_timestamp_seconds", labels, bucket["last_success"])
                    )
                    last_duration.append(
                        render_sample("august_semaphore_last_success_duration_seconds", labels, bucket["last_success_duration"])
                    )
                durations = bucket["durations"]
                count_samples.append(render_sample("august_semaphore_task_duration_samples", labels, len(durations)))
                if bucket["oldest_end"] is not None:
                    coverage_samples.append(
                        render_sample(
                            "august_semaphore_oldest_task_timestamp_seconds", labels, bucket["oldest_end"]
                        )
                    )
                for status in sorted(bucket["statuses"]):
                    status_samples.append(
                        render_sample(
                            "august_semaphore_tasks", dict(labels, status=status), bucket["statuses"][status]
                        )
                    )
                for fraction in QUANTILES:
                    value = quantile(durations, fraction)
                    if value is not None:
                        quantile_samples.append(
                            render_sample("august_semaphore_task_duration_seconds", dict(labels, quantile=f"{fraction:g}"), value)
                        )

        def family(name: str, help_text: str, samples: list[str]) -> None:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} gauge")
            lines.extend(samples)

        family("august_semaphore_scrape_ok", "1 when the last read of the Semaphore task list succeeded.", ok_samples)
        family(
            "august_semaphore_response_contract_ok",
            "0 when the API answered in a shape this exporter does not understand. No durations are "
            "published in that case, rather than zeroes that read as an empty history.",
            contract_samples,
        )
        family(
            "august_semaphore_tasks",
            "Tasks seen in the window, by status. A template whose recent runs all failed has no "
            "successful duration, and that must not look like no data.",
            status_samples,
        )
        family(
            "august_semaphore_last_success_timestamp_seconds",
            "When the last successful run of this template finished.",
            last_success,
        )
        family(
            "august_semaphore_last_success_duration_seconds",
            "How long the last successful run of this template took.",
            last_duration,
        )
        family(
            "august_semaphore_task_duration_seconds",
            "Duration percentile over recent successful runs of this template.",
            quantile_samples,
        )
        family(
            "august_semaphore_task_duration_samples",
            "How many successful runs those percentiles were computed from.",
            count_samples,
        )
        family(
            "august_semaphore_tasks_read",
            "How many tasks the last collection read. Coverage metadata: a percentile over 20 tasks "
            "is a percentile over 20 tasks.",
            [line for line in coverage_samples if line.startswith("august_semaphore_tasks_read")],
        )
        family(
            "august_semaphore_pages_read",
            "How many pages of the task list were followed.",
            [line for line in coverage_samples if line.startswith("august_semaphore_pages_read")],
        )
        family(
            "august_semaphore_history_truncated",
            "1 when the read stopped at the limit or the page cap instead of at the end of history.",
            [line for line in coverage_samples if line.startswith("august_semaphore_history_truncated")],
        )
        family(
            "august_semaphore_window_seconds",
            "The window durations are summarised over; 0 means no window.",
            [line for line in coverage_samples if line.startswith("august_semaphore_window_seconds")],
        )
        family(
            "august_semaphore_oldest_task_timestamp_seconds",
            "The oldest task the read reached, per template. How far back the numbers actually go.",
            [line for line in coverage_samples if line.startswith("august_semaphore_oldest_task_timestamp_seconds")],
        )
        family(
            "august_semaphore_exporter_scrape_duration_seconds",
            "How long this collection took.",
            [render_sample("august_semaphore_exporter_scrape_duration_seconds", {}, time.time() - started)],
        )
        return "\n".join(lines) + "\n"


class MetricsHandler(BaseHTTPRequestHandler):
    collector: Collector

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path.split("?")[0] not in ("/metrics", "/"):
            self.send_error(404)
            return
        body = self.collector.collect().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--projects", default="1", help="comma separated Semaphore project ids")
    parser.add_argument("--limit", type=int, default=200, help="how many recent tasks to read per project")
    parser.add_argument("--window-days", type=float, default=90.0, help="0 disables the window")
    parser.add_argument("--listen-address", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=9302)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--once", action="store_true")
    arguments = parser.parse_args(argv)

    base = os.environ.get("SEMAPHORE_API_BASE", "")
    token = os.environ.get("SEMAPHORE_API_TOKEN", "")
    client = SemaphoreClient(base, token, timeout=arguments.timeout) if base and token else None
    projects = [int(part) for part in arguments.projects.split(",") if part.strip()]
    window = None if arguments.window_days <= 0 else arguments.window_days * 86400
    collector = Collector(client, projects, arguments.limit, window)

    if arguments.once:
        sys.stdout.write(collector.collect())
        return 0

    handler = type("BoundMetricsHandler", (MetricsHandler,), {"collector": collector})
    ThreadingHTTPServer((arguments.listen_address, arguments.listen_port), handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
