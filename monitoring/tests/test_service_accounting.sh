#!/usr/bin/env bash
# The accounting contract, evaluated by the real PromQL engine.
#
# Everything else about these rules can be checked by reading them. This cannot:
# whether one gigabit of user traffic that crosses a bridge shows up as one
# gigabit of service usage or as two depends on how three joins and a
# subtraction interact, and the only honest way to know is to feed synthetic
# counters to Prometheus's own evaluator and look at the answer.
#
# It runs against monitoring/prometheus/recording-rules.yml itself - the file
# that is copied to the monitoring server, byte for byte - so there is no
# rendering step between what is tested and what runs.
set -euo pipefail

monitoring="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

if ! command -v promtool >/dev/null 2>&1; then
  echo "NOT VERIFIED: promtool is not installed, so the accounting contract was not evaluated." >&2
  echo "              Install it from the pinned Prometheus release and re-run." >&2
  exit 0
fi

sed "s|RULE_DIR|$monitoring/prometheus|g" \
  "$monitoring/tests/promtool/service_accounting.test.yml.in" > "$work/service_accounting.test.yml"

promtool test rules "$work/service_accounting.test.yml"

echo "One user gigabit through a bridge is one gigabit of service usage, not two."
