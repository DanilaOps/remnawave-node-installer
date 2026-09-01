#!/usr/bin/env bash
# Check the monitoring-server configuration this repository ships.
#
# Nothing here is installed by Ansible any more: the operator copies these
# files to the monitoring server by hand. That makes this check more important,
# not less - a rule file with a typo in it used to be caught by a failed
# playbook run, and now the only thing between it and a Prometheus that refuses
# to start is CI.
#
# Every file is checked as it sits in the repository, with no rendering step,
# so what CI accepted is byte for byte what gets copied.
set -euo pipefail

monitoring="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

# --- the rules -------------------------------------------------------------
if command -v promtool >/dev/null 2>&1; then
  # --lint-fatal so that a duplicate record name, which Prometheus tolerates
  # and which silently overwrites one series with another, fails here.
  promtool check rules --lint-fatal \
    "$monitoring/prometheus/recording-rules.yml" \
    "$monitoring/prometheus/alert-rules.yml"

  # The example prometheus.yml, with its placeholders filled in and its file
  # references pointed at this workspace, so that the shape of the config is
  # checked even though the real one is written by hand on the server.
  mkdir -p "$work/rules" "$work/targets"
  cp "$monitoring/prometheus/recording-rules.yml" "$work/rules/"
  cp "$monitoring/prometheus/alert-rules.yml" "$work/rules/"
  cp "$monitoring/prometheus/targets/nodes.json.example" "$work/targets/nodes.json"
  cp "$monitoring/prometheus/targets/blackbox.json.example" "$work/targets/blackbox.json"
  printf 'placeholder\n' > "$work/remnawave-metrics.password"
  sed -e "s|/etc/prometheus/rules|$work/rules|g" \
      -e "s|/etc/prometheus/targets|$work/targets|g" \
      -e "s|/etc/prometheus/remnawave-metrics.password|$work/remnawave-metrics.password|g" \
      -e "s|<METRICS_USER>|metrics|g" \
      -e "s|<PANEL_HOST>|panel.example.test|g" \
      "$monitoring/prometheus/prometheus.yml.example" > "$work/prometheus.yml"
  promtool check config --lint-fatal "$work/prometheus.yml"
else
  echo "NOT VERIFIED: promtool is not installed, so the rules were checked structurally only" >&2
fi

if command -v amtool >/dev/null 2>&1; then
  printf 'placeholder\n' > "$work/telegram.token"
  sed -e "s|/etc/alertmanager/telegram.token|$work/telegram.token|g" \
      -e "s|<TELEGRAM_CHAT_ID>|-1001234567890|g" \
      "$monitoring/alertmanager/alertmanager.yml.example" > "$work/alertmanager.yml"
  amtool check-config "$work/alertmanager.yml"
else
  echo "NOT VERIFIED: amtool is not installed, so alertmanager.yml was checked structurally only" >&2
fi

# --- structure, whether or not the upstream tools are here -----------------
python3 - "$monitoring" <<'PY'
import json
import re
import sys
from pathlib import Path

import yaml

monitoring = Path(sys.argv[1])

config = yaml.safe_load((monitoring / "prometheus/prometheus.yml.example").read_text(encoding="utf-8"))
jobs = {job["job_name"] for job in config["scrape_configs"]}
for required in ("remnawave", "august_node_exporter", "august_capacity", "august_blackbox"):
    assert required in jobs, f"scrape job {required} is missing"

# A password that is a value rather than a file path ends up in the answer of
# /api/v1/status/config, which any reader of Prometheus can fetch.
for job in config["scrape_configs"]:
    basic = job.get("basic_auth") or {}
    assert "password" not in basic, f"{job['job_name']} carries a literal password"

for name in ("recording-rules.yml", "alert-rules.yml"):
    document = yaml.safe_load((monitoring / "prometheus" / name).read_text(encoding="utf-8"))
    assert document["groups"], f"{name} has no rule groups"
    for group in document["groups"]:
        for rule in group["rules"]:
            assert "record" in rule or "alert" in rule, f"a rule in {name} is neither"

# No Ansible variable left over from the days when these were templates. Both
# Prometheus and Alertmanager use Go templating, so "{{" on its own proves
# nothing: a Go expression starts with $ or . or a builtin, while a leftover
# Ansible variable is a bare snake_case name - and that is a rule threshold
# that silently became the literal text "{{ capacity_red_for }}".
GO_BUILTINS = {
    "range", "end", "if", "else", "with", "printf", "humanize", "len",
    "humanizeDuration", "humanizePercentage", "template", "define", "block",
}
LEFTOVER = re.compile(r"\{\{-?\s*([A-Za-z_][A-Za-z0-9_]*)")
for path in list((monitoring / "prometheus").rglob("*.yml")) + [
    monitoring / "alertmanager/alertmanager.yml.example",
    monitoring / "blackbox/blackbox.yml",
]:
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        for match in LEFTOVER.finditer(line):
            name = match.group(1)
            if name not in GO_BUILTINS:
                raise AssertionError(
                    f"{path.name}:{number} still carries an Ansible variable: {{{{ {name} ... }}}}"
                )

json.loads((monitoring / "grafana/august-capacity.json").read_text(encoding="utf-8"))
json.loads((monitoring / "prometheus/targets/nodes.json.example").read_text(encoding="utf-8"))
json.loads((monitoring / "prometheus/targets/blackbox.json.example").read_text(encoding="utf-8"))

print(f"prometheus scrape jobs: {sorted(jobs)}")
PY

echo "The monitoring-server configuration in this repository is valid."
