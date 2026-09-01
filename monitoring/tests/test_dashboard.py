#!/usr/bin/env python3
"""Structural tests for the Grafana dashboard this repository ships.

A dashboard is JSON, so nothing fails loudly when a panel queries a series
that no exporter and no recording rule ever produces: the panel just says
"No data" forever and the operator believes the fleet is quiet.  These tests
tie every ``august`` series the dashboard asks for back to something in this
repository that actually publishes it, and check the structural promises the
owner asked for (nine named rows, five variables that are really applied,
missing data rendered as missing rather than as zero).
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

MONITORING = Path(__file__).resolve().parents[1]
REPO = MONITORING.parent
DASHBOARD = MONITORING / "grafana/august-capacity.json"
RECORDING_RULES = MONITORING / "prometheus/recording-rules.yml"
ALERT_RULES = MONITORING / "prometheus/alert-rules.yml"
NODE_MONITORING = REPO / "ansible/roles/node_monitoring"

EXPECTED_ROW_PREFIXES = (
    "1. Global capacity",
    "2. Pool status",
    "3. Pool capacity charts",
    "4. Nodes",
    "5. Bridges",
    "6. Connections",
    "7. Quotas",
    "8. Scaling and recommendation",
    "9. Infrastructure and data quality",
)

# Variables the owner requires, and the label each one must filter on so that
# selecting a value in the dashboard actually narrows the queries.
REQUIRED_VARIABLES = {
    "environment": None,
    "pool": "pool",
    "country": "country",
    "node": "node",
    "bridge": "name",
}

IDENTIFIER = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
QUOTED = re.compile(r'"[^"]*"')


def series_referenced(expr: str) -> set[str]:
    """Series names in a PromQL expression.

    Quoted strings are removed first: a job name (job="august_node_exporter"),
    a label_replace replacement and an alert's regex are text, not series, and
    counting them as series would make this test complain about names that
    were never meant to exist.
    """
    bare = QUOTED.sub('""', expr)
    return {token for token in IDENTIFIER.findall(bare) if token.startswith("august")}


def load_dashboard() -> dict:
    return json.loads(DASHBOARD.read_text(encoding="utf-8"))


def iter_panels(dashboard: dict):
    stack = list(dashboard.get("panels", []))
    while stack:
        panel = stack.pop()
        yield panel
        stack.extend(panel.get("panels", []) or [])


def panel_expressions(dashboard: dict) -> list[tuple[str, str]]:
    found = []
    for panel in iter_panels(dashboard):
        for target in panel.get("targets", []) or []:
            expr = target.get("expr")
            if expr:
                found.append((panel.get("title", panel.get("type", "?")), expr))
    return found


def variable_queries(dashboard: dict) -> list[str]:
    queries = []
    for variable in dashboard.get("templating", {}).get("list", []):
        query = variable.get("query")
        if isinstance(query, str):
            queries.append(query)
        elif isinstance(query, dict) and isinstance(query.get("query"), str):
            queries.append(query["query"])
    return queries


def produced_series() -> set[str]:
    """Every august series this repository publishes or records."""
    names: set[str] = set()

    for path in (RECORDING_RULES,):
        for line in path.read_text(encoding="utf-8").splitlines():
            match = re.match(r"\s*-?\s*record:\s*(\S+)", line)
            if match:
                names.add(match.group(1).strip("\"'"))

    # Exporters and the node collector write exposition text.  Take every
    # august_* identifier that appears at the start of an exposition line or
    # inside a metric-name string in the Python sources.
    sources = list(MONITORING.glob("*.py"))
    sources += list(NODE_MONITORING.rglob("*.j2"))
    sources += list(NODE_MONITORING.rglob("*.yml"))
    for path in sources:
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"august_[a-zA-Z0-9_]+", text):
            names.add(match.group(0))
    return names


class RowTests(unittest.TestCase):
    def test_nine_rows_in_the_required_order(self):
        dashboard = load_dashboard()
        rows = [p["title"] for p in dashboard["panels"] if p.get("type") == "row"]
        self.assertEqual(len(rows), 9, rows)
        for actual, expected in zip(rows, EXPECTED_ROW_PREFIXES):
            self.assertTrue(
                actual.startswith(expected),
                f"row {actual!r} does not start with {expected!r}",
            )

    def test_every_row_has_panels_under_it(self):
        dashboard = load_dashboard()
        panels = dashboard["panels"]
        row_indexes = [i for i, p in enumerate(panels) if p.get("type") == "row"]
        for position, index in enumerate(row_indexes):
            end = row_indexes[position + 1] if position + 1 < len(row_indexes) else len(panels)
            nested = panels[index].get("panels") or []
            self.assertGreater(
                len(nested) + (end - index - 1),
                0,
                f"row {panels[index]['title']!r} is empty",
            )

    def test_panels_are_not_all_in_one_row(self):
        dashboard = load_dashboard()
        self.assertGreaterEqual(len(dashboard["panels"]), 30)


class VariableTests(unittest.TestCase):
    def test_required_variables_exist(self):
        dashboard = load_dashboard()
        names = [v["name"] for v in dashboard["templating"]["list"]]
        for required in REQUIRED_VARIABLES:
            self.assertIn(required, names)

    def test_variables_are_applied_in_queries(self):
        dashboard = load_dashboard()
        exprs = [expr for _, expr in panel_expressions(dashboard)]
        joined = "\n".join(exprs)
        for name, label in REQUIRED_VARIABLES.items():
            if label is None:
                continue
            pattern = re.compile(rf'{label}\s*=~\s*"\$\{{?{name}\}}?"')
            self.assertTrue(
                pattern.search(joined),
                f"variable ${name} is never applied as {label}=~ in any panel query",
            )

    def test_environment_variable_is_a_real_selector(self):
        dashboard = load_dashboard()
        environment = next(
            v for v in dashboard["templating"]["list"] if v["name"] == "environment"
        )
        self.assertIn(environment["type"], {"custom", "constant", "textbox", "datasource", "query"})
        self.assertTrue(environment.get("current") or environment.get("options"))

    def test_dependent_variables_are_filtered_by_pool(self):
        dashboard = load_dashboard()
        queries = "\n".join(variable_queries(dashboard))
        self.assertIn("$pool", queries, "no variable query narrows by the selected pool")

    def test_multi_value_variables_include_all(self):
        dashboard = load_dashboard()
        for variable in dashboard["templating"]["list"]:
            if variable["name"] in {"pool", "node", "bridge", "country"}:
                self.assertTrue(
                    variable.get("includeAll"),
                    f"variable {variable['name']} cannot be set to All",
                )
                self.assertTrue(variable.get("multi"), variable["name"])


class SeriesTests(unittest.TestCase):
    def test_every_queried_series_is_produced_somewhere(self):
        dashboard = load_dashboard()
        produced = produced_series()
        missing: dict[str, set[str]] = {}
        for title, expr in panel_expressions(dashboard):
            for token in series_referenced(expr):
                if token not in produced:
                    missing.setdefault(token, set()).add(title)
        self.assertEqual(
            missing,
            {},
            "dashboard queries series that nothing in this repository publishes: "
            + json.dumps({k: sorted(v) for k, v in missing.items()}, indent=2),
        )

    def test_variable_queries_use_produced_series(self):
        produced = produced_series()
        for query in variable_queries(load_dashboard()):
            for token in series_referenced(query):
                self.assertIn(token, produced, f"variable query uses unknown {token}")

    def test_no_recording_rule_is_referenced_before_it_exists(self):
        # Recording rules referenced by the dashboard must be recorded, not
        # merely mentioned inside an alert expression.
        recorded = set()
        for line in RECORDING_RULES.read_text(encoding="utf-8").splitlines():
            match = re.match(r"\s*-?\s*record:\s*(\S+)", line)
            if match:
                recorded.add(match.group(1).strip("\"'"))
        for title, expr in panel_expressions(load_dashboard()):
            for token in series_referenced(expr):
                if token.startswith("august:"):
                    self.assertIn(token, recorded, f"panel {title!r} uses unrecorded {token}")


class HonestyTests(unittest.TestCase):
    def test_first_connected_is_named_first_observed_connected(self):
        text = DASHBOARD.read_text(encoding="utf-8")
        self.assertNotIn("august_node_first_connected", text)
        self.assertIn("august_node_first_observed_connected_timestamp_seconds", text)

    def test_first_observed_panel_states_the_limitation(self):
        dashboard = load_dashboard()
        for panel in iter_panels(dashboard):
            exprs = [t.get("expr", "") for t in panel.get("targets", []) or []]
            if any("first_observed_connected" in expr for expr in exprs):
                description = (panel.get("description") or "").lower()
                self.assertTrue(
                    "observ" in description or "first saw" in description,
                    f"panel {panel.get('title')!r} does not explain that the value is only when monitoring first saw the node connected",
                )
                return
        self.fail("no panel uses august_node_first_observed_connected_timestamp_seconds")

    def test_packet_loss_is_not_claimed(self):
        dashboard = load_dashboard()
        for panel in iter_panels(dashboard):
            title = (panel.get("title") or "").lower()
            if "packet loss" in title or "loss" in title:
                description = (panel.get("description") or "").lower()
                self.assertTrue(
                    "not measured" in description or "no " in description,
                    f"panel {panel.get('title')!r} claims a packet loss figure",
                )

    def test_missing_data_is_not_rendered_as_zero(self):
        dashboard = load_dashboard()
        checked = 0
        for panel in iter_panels(dashboard):
            if panel.get("type") in {"row", "text"}:
                continue
            defaults = (
                panel.get("fieldConfig", {}).get("defaults", {})
            )
            no_value = defaults.get("noValue")
            self.assertIsNotNone(
                no_value,
                f"panel {panel.get('title')!r} has no noValue, so missing data can read as zero",
            )
            self.assertNotEqual(str(no_value).strip(), "0", panel.get("title"))
            checked += 1
        self.assertGreater(checked, 20)

    def test_uncertain_capacity_is_visible(self):
        text = DASHBOARD.read_text(encoding="utf-8")
        for series in (
            "august_pool_capacity_certain",
            "august_service_capacity_certain",
            "august_capacity_shared_pool",
            "august_capacity_unrated",
        ):
            self.assertIn(series, text, f"{series} is not shown anywhere")

    def test_service_and_physical_capacity_are_both_shown_and_distinguished(self):
        dashboard = load_dashboard()
        titles = {(p.get("title") or "").lower() for p in iter_panels(dashboard)}
        self.assertTrue(
            any("service" in title for title in titles),
            "no panel is about service capacity",
        )
        self.assertTrue(
            any("physical" in title or "infrastructure" in title for title in titles),
            "no panel separates the physical/infrastructure figure",
        )
        for panel in iter_panels(dashboard):
            exprs = " ".join(t.get("expr", "") for t in panel.get("targets", []) or [])
            if "august:physical_usage_bps" in exprs:
                description = (panel.get("description") or "").lower()
                self.assertTrue(
                    "diagnost" in description or "not the service" in description or "double" in description,
                    f"panel {panel.get('title')!r} shows the physical figure without saying it is a diagnostic",
                )

    def test_scaling_recommendation_is_ordered_deterministically(self):
        dashboard = load_dashboard()
        for panel in iter_panels(dashboard):
            exprs = " ".join(t.get("expr", "") for t in panel.get("targets", []) or [])
            if "august:scaling_rank" in exprs:
                self.assertIn("sort", exprs, "the recommendation table is not sorted")
                return
        self.fail("no panel presents august:scaling_rank")

    def test_dashboard_declares_a_schema_and_a_uid(self):
        dashboard = load_dashboard()
        self.assertTrue(dashboard.get("uid"))
        self.assertTrue(dashboard.get("title"))
        self.assertIsInstance(dashboard.get("schemaVersion"), int)

    def test_no_stale_node_names(self):
        text = DASHBOARD.read_text(encoding="utf-8")
        for stale in ("SE-01", "fr02", "tr02", "potential_capacity"):
            self.assertNotIn(stale, text, f"dashboard still mentions {stale}")


class DocumentationTests(unittest.TestCase):
    """Documentation and role assertions may not name series that do not exist.

    A doc that promises `august_fleet_active_capacity_mbps` sends an operator
    looking for a series nobody publishes; a role assertion that checks for one
    fails a deployment for no reason. Both happened once, which is why this is
    a test.
    """

    DOCS = (
        MONITORING / "capacity/README.md",
        MONITORING / "README.ru.md",
        REPO / "ansible/MONITORING.ru.md",
    )
    ROLE_TASKS = tuple((NODE_MONITORING / "tasks").glob("*.yml"))

    @staticmethod
    def _rule_group_names() -> set[str]:
        """Rule group names are legitimate things for a doc to point at."""
        names: set[str] = set()
        for path in (RECORDING_RULES, ALERT_RULES):
            for line in path.read_text(encoding="utf-8").splitlines():
                match = re.match(r"\s*-\s*name:\s*(\S+)", line)
                if match:
                    names.add(match.group(1).strip("\"'"))
        return names

    @staticmethod
    def _scrape_job_names() -> set[str]:
        """So are Prometheus scrape job names, which also start with august_."""
        text = (MONITORING / "prometheus/prometheus.yml.example").read_text(encoding="utf-8")
        return set(re.findall(r"job_name:\s*(\S+)", text))

    def test_documentation_names_only_series_that_exist(self):
        produced = produced_series() | self._rule_group_names() | self._scrape_job_names()
        missing: dict[str, list[str]] = {}
        for path in self.DOCS:
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r"august[:_][a-zA-Z0-9_:]+", text):
                name = match.group(0).rstrip("_:")
                # A doc may legitimately write a family with a trailing wildcard.
                if name.endswith("*"):
                    continue
                if name not in produced:
                    missing.setdefault(name, []).append(path.name)
        self.assertEqual(
            missing,
            {},
            "documentation names series nothing publishes: "
            + json.dumps(missing, indent=2),
        )

    def test_role_assertions_name_only_series_that_exist(self):
        produced = produced_series()
        missing: dict[str, list[str]] = {}
        for path in self.ROLE_TASKS:
            for line in path.read_text(encoding="utf-8").splitlines():
                if "august" not in line or line.lstrip().startswith("#"):
                    continue
                for match in re.finditer(r"august[:_][a-zA-Z0-9_:]+", line):
                    name = match.group(0).rstrip("_:")
                    if name not in produced:
                        missing.setdefault(name, []).append(path.name)
        self.assertEqual(
            missing,
            {},
            "the role checks for series nothing publishes: " + json.dumps(missing, indent=2),
        )

    def test_alert_names_in_the_documentation_exist(self):
        declared = set(
            re.findall(r"alert:\s*(\w+)", ALERT_RULES.read_text(encoding="utf-8"))
        )
        for path in self.DOCS:
            text = path.read_text(encoding="utf-8")
            for name in re.findall(r"\bAugust[A-Z]\w+", text):
                self.assertIn(
                    name,
                    declared,
                    f"{path.name} names an alert that does not exist: {name}",
                )


class GeneratorTests(unittest.TestCase):
    def test_committed_json_matches_the_generator(self):
        """The JSON is a build output; a hand edit to it would be lost."""
        import subprocess
        import sys
        import tempfile

        before = DASHBOARD.read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / "august-capacity.json"
            backup.write_bytes(before)
            result = subprocess.run(
                [sys.executable, str(REPO / "monitoring/build_dashboard.py")],
                capture_output=True,
                text=True,
            )
            after = DASHBOARD.read_bytes()
            DASHBOARD.write_bytes(before)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            after,
            before,
            "the committed dashboard differs from what build_dashboard.py produces; "
            "regenerate it instead of editing the JSON",
        )


class AlertCrossReferenceTests(unittest.TestCase):
    def test_alerts_only_use_produced_series(self):
        produced = produced_series()
        text = ALERT_RULES.read_text(encoding="utf-8")
        # Only look at expr: lines so that annotations are not scanned.
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("expr:"):
                continue
            for token in series_referenced(stripped):
                self.assertIn(token, produced, f"alert uses unknown series {token}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
