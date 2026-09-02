#!/usr/bin/env bash
# What may and may not page somebody, evaluated by the real PromQL engine.
#
# Two things are being proved, and neither can be established by reading the
# rules. First, that a threshold expressed in Mbit/s behaves the same on a
# 5 Gbit/s link and a 1 Gbit/s link - the reason the model stopped using a
# percentage. Second, that the alerts this fleet deliberately does not want,
# because a different system already sends them, are actually gone rather than
# merely renamed: the fixture feeds a node that is down, disabled, unknown to
# the panel and newly added, and asserts that the metrics are still published
# and that not one alert fires.
#
# It runs against monitoring/prometheus/*.yml themselves - the files copied to
# the monitoring server, byte for byte - so there is no rendering step between
# what is tested and what runs.
set -euo pipefail

monitoring="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

if ! command -v promtool >/dev/null 2>&1; then
  echo "NOT VERIFIED: promtool is not installed, so the alert thresholds were not evaluated." >&2
  echo "              Install it from the pinned Prometheus release and re-run." >&2
  exit 0
fi

sed "s|RULE_DIR|$monitoring/prometheus|g" \
  "$monitoring/tests/promtool/alert_thresholds.test.yml.in" > "$work/alert_thresholds.test.yml"

promtool test rules "$work/alert_thresholds.test.yml"

echo "Bandwidth pages on Mbit/s free, not on a percentage, and the duplicated alerts are gone."
