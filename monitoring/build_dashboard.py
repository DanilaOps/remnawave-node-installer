#!/usr/bin/env python3
"""Build monitoring/grafana/august-capacity.json.

The dashboard is a hundred kilobytes of JSON.  Hand-editing it is how a panel
ends up querying a series nobody publishes, so it is generated from this file
and the JSON is committed as the build output.  Change a panel here, run this
script, and run monitoring/tests/test_dashboard.py, which checks that every
series the dashboard asks for is really produced by a recording rule or an
exporter in this repository.

Usage:  python3 monitoring/build_dashboard.py
"""

import json
import pathlib

DS = {"type": "prometheus", "uid": "august-prometheus"}
STATUS_MAP = [{"type": "value", "options": {
    "0": {"text": "RED", "color": "red", "index": 0},
    "1": {"text": "YELLOW", "color": "orange", "index": 1},
    "2": {"text": "GREEN", "color": "green", "index": 2}}}]
BOOL_MAP = [{"type": "value", "options": {
    "0": {"text": "no", "color": "text", "index": 0},
    "1": {"text": "yes", "color": "green", "index": 1}}}]
UNCERTAIN_MAP = [{"type": "value", "options": {
    "0": {"text": "aggregate / uncertain", "color": "orange", "index": 0},
    "1": {"text": "attributed", "color": "green", "index": 1}}}]

# Every query is filtered by the variables, so a variable that is set actually
# narrows the panel. A dashboard with variables that do nothing is worse than one
# with none: it invites the reader to believe a filter was applied.
POOL = 'pool=~"$pool"'
NODE = 'node=~"$node"'
BRIDGE = 'name=~"$bridge"'

panels = []
y = 0

def target(expr, legend="", instant=False, fmt="time_series", ref="A"):
    return {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": ref,
            "instant": instant, "format": fmt, "range": not instant}

def stat(title, expr, unit, x, w=4, h=4, decimals=None, mappings=None, desc=""):
    global y
    return {"type": "stat", "title": title, "description": desc, "datasource": DS,
            "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "targets": [target(expr, instant=True)],
            "fieldConfig": {"defaults": {"unit": unit, "decimals": decimals,
                                         "mappings": mappings or [],
                                         "noValue": "no data"}, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "colorMode": "value", "graphMode": "none", "textMode": "auto"}}

def table(title, targets, h=9, desc="", overrides=None):
    global y
    return {"type": "table", "title": title, "description": desc, "datasource": DS,
            "gridPos": {"x": 0, "y": y, "w": 24, "h": h},
            "targets": targets,
            "transformations": [{"id": "merge", "options": {}}],
            "fieldConfig": {"defaults": {"noValue": "no data"}, "overrides": overrides or []},
            "options": {"showHeader": True, "footer": {"show": False}}}

def ts(title, targets, x, w=12, h=8, unit="bps", desc=""):
    global y
    return {"type": "timeseries", "title": title, "description": desc, "datasource": DS,
            "gridPos": {"x": x, "y": y, "w": w, "h": h}, "targets": targets,
            "fieldConfig": {"defaults": {"unit": unit, "noValue": "no data",
                                         "custom": {"fillOpacity": 8, "lineWidth": 1,
                                                    "spanNulls": False,
                                                    "insertNulls": 300000}},
                            "overrides": []},
            "options": {"legend": {"displayMode": "table", "placement": "bottom",
                                   "calcs": ["lastNotNull", "max"]},
                        "tooltip": {"mode": "multi", "sort": "desc"}}}

def row(title, desc=""):
    global y
    p = {"type": "row", "title": title, "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
         "collapsed": False, "panels": [], "description": desc}
    y += 1
    return p

# Free headroom is coloured on the operational scale the alerts use: at or
# above 500 Mbit/s green, 200 to 500 amber, below 200 red. Deliberately not a
# percentage - 20% of a 5 Gbit/s link is a gigabit still free and 20% of a
# 1 Gbit/s link is the emergency, and one colour scale cannot mean both. The
# percentage columns stay beside these, because the ratio is what tells a human
# whether 170 Mbit/s is a rounding error or the whole link.
MBPS_THRESHOLDS = {"mode": "absolute", "steps": [
    {"color": "red", "value": None},
    {"color": "orange", "value": 200},
    {"color": "green", "value": 500}]}

status_override = [
    {"matcher": {"id": "byRegexp", "options": ".*status.*"},
     "properties": [{"id": "mappings", "value": STATUS_MAP}]},
    # Absolute headroom, in Mbit/s, coloured by the alert thresholds. noValue
    # stays "N/A" rather than 0: an unmeasured node has no headroom figure, and
    # rendering that as red would be inventing a capacity nobody measured.
    {"matcher": {"id": "byRegexp", "options": ".*free_mbps.*"},
     "properties": [{"id": "unit", "value": "Mbits"},
                    {"id": "decimals", "value": 0},
                    {"id": "noValue", "value": "N/A"},
                    {"id": "thresholds", "value": MBPS_THRESHOLDS},
                    {"id": "custom.cellOptions",
                     "value": {"type": "color-text"}}]},
    {"matcher": {"id": "byRegexp", "options": ".*(ratio|free_pct).*"},
     "properties": [{"id": "unit", "value": "percentunit"}, {"id": "decimals", "value": 1}]},
    {"matcher": {"id": "byRegexp", "options": ".*(certain|attributed).*"},
     "properties": [{"id": "mappings", "value": UNCERTAIN_MAP}]},
    {"matcher": {"id": "byRegexp", "options": "^(active|enabled|connected|disabled|mapped|declared|shared).*"},
     "properties": [{"id": "mappings", "value": BOOL_MAP}]},
]

# ---------------------------------------------------------------- row 1
panels.append(row("1. Global capacity — service, not infrastructure",
    "Service capacity and usage count each user gigabit once: a flow through a bridge is not "
    "counted at both of its ends. The physical figures next to them count every leg and exist for "
    "planning the plumbing, never as the fleet KPI."))
panels.append(stat("Service capacity ↓", 'sum(august_service_capacity_mbps{direction="download"}) * 1e6', "bps", 0,
                   desc="Summed over the pools users connect to."))
panels.append(stat("Service usage ↓", 'august:service_usage_bps{direction="download"}', "bps", 4,
                   desc="Physical usage minus the duplication the bridge counters measure."))
panels.append(stat("Service free ↓", 'august:service_free_ratio{direction="download"}', "percentunit", 8, decimals=1))
panels.append(stat("Service capacity ↑", 'sum(august_service_capacity_mbps{direction="upload"}) * 1e6', "bps", 12))
panels.append(stat("Service usage ↑", 'august:service_usage_bps{direction="upload"}', "bps", 16))
panels.append(stat("Service free ↑", 'august:service_free_ratio{direction="upload"}', "percentunit", 20, decimals=1))
y += 4
panels.append(stat("Attribution", 'min(august_service_capacity_certain)', "none", 0, mappings=UNCERTAIN_MAP,
                   desc="Aggregate means at least one pool is rated as a location and a member is missing, so the "
                        "figure above is an upper bound."))
panels.append(stat("Physical capacity ↓ (diagnostic)", 'sum(august_physical_capacity_mbps{direction="download"}) * 1e6', "bps", 4,
                   desc="Every leg of the infrastructure added up, bridges included. A diagnostic for the "
                        "plumbing, not the capacity of the service: a bridged flow occupies two legs."))
panels.append(stat("Physical usage ↓ (diagnostic)", 'august:physical_usage_bps{direction="download"}', "bps", 8,
                   desc="The sum of node counters, in which one bridged user flow appears twice. A "
                        "diagnostic. The service figure to the left is the one the fleet is judged by."))
panels.append(stat("Bridged share ↓", 'august:bridge_usage_total_bps{direction="download"}', "bps", 12,
                   desc="The part of physical usage that is one user flow counted twice."))
panels.append(stat("Nodes active", f'sum(august_node_active{{{POOL}}})', "none", 16))
panels.append(stat("Nodes disabled", f'sum(august_node_administratively_disabled{{{POOL}}})', "none", 20,
                   desc="Deliberate. Out of capacity, and not an incident."))
y += 4
panels.append(ts("Service vs physical usage ↓", [
    dict(target('august:service_usage_bps{direction="download"}', "service"), refId="A"),
    dict(target('august:physical_usage_bps{direction="download"}', "physical"), refId="B"),
    dict(target('august:bridge_usage_total_bps{direction="download"}', "bridged (double counted)"), refId="C"),
], 0, desc="Service is the fleet KPI: one user flow counted once. Physical is a diagnostic for the plumbing - it counts the same flow on the ingress node and again on the exit node, and the bridged series is exactly that double count."))
panels.append(ts("Service vs physical usage ↑", [
    dict(target('august:service_usage_bps{direction="upload"}', "service"), refId="A"),
    dict(target('august:physical_usage_bps{direction="upload"}', "physical"), refId="B"),
    dict(target('august:bridge_usage_total_bps{direction="upload"}', "bridged (double counted)"), refId="C"),
], 12, desc="Service is the fleet KPI: one user flow counted once. Physical is a diagnostic for the plumbing - it counts the same flow on the ingress node and again on the exit node, and the bridged series is exactly that double count."))
y += 8

# ---------------------------------------------------------------- row 2
panels.append(row("2. Pool status",
    "A pool's own resource figure. Where a pool is rated as a location rather than per machine, "
    "'attributed' reads aggregate and the free percentage is an upper bound."))
panels.append(table("Pools", [
    dict(target(f'august_pool_capacity_mbps{{{POOL},direction="download"}}', instant=True, fmt="table"), refId="A"),
    dict(target(f'august_pool_capacity_mbps{{{POOL},direction="upload"}}', instant=True, fmt="table"), refId="B"),
    dict(target(f'august:pool_used_bps{{{POOL},direction="download"}} / 1e6', instant=True, fmt="table"), refId="C"),
    dict(target(f'august:alert_pool_free_mbps{{{POOL},direction="download"}}', instant=True, fmt="table"), refId="D"),
    dict(target(f'august:alert_pool_free_mbps{{{POOL},direction="upload"}}', instant=True, fmt="table"), refId="H"),
    dict(target(f'august:pool_free_ratio{{{POOL},direction="download"}}', instant=True, fmt="table"), refId="E"),
    dict(target(f'august:pool_free_ratio{{{POOL},direction="upload"}}', instant=True, fmt="table"), refId="F"),
    dict(target(f'august:headroom_status_worst{{{POOL}}}', instant=True, fmt="table"), refId="G"),
    dict(target(f'august_pool_active_members{{{POOL}}}', instant=True, fmt="table"), refId="H"),
    dict(target(f'august_pool_members{{{POOL}}}', instant=True, fmt="table"), refId="I"),
    dict(target(f'august_pool_capacity_certain{{{POOL},direction="download"}}', instant=True, fmt="table"), refId="J"),
    dict(target(f'august_pool_capacity_aggregate{{{POOL},direction="download"}}', instant=True, fmt="table"), refId="K"),
    dict(target(f'august_pool_serves_users{{{POOL}}}', instant=True, fmt="table"), refId="L"),
], h=10, overrides=status_override))
y += 10

# ---------------------------------------------------------------- row 3
panels.append(row("3. Pool capacity charts"))
panels.append(ts("Pool usage ↓", [dict(target(f'august:pool_used_bps{{{POOL},direction="download"}}', "{{pool}}"), refId="A")], 0))
panels.append(ts("Pool usage ↑", [dict(target(f'august:pool_used_bps{{{POOL},direction="upload"}}', "{{pool}}"), refId="A")], 12))
y += 8
panels.append(ts("Pool free capacity ↓", [dict(target(f'august:pool_free_ratio{{{POOL},direction="download"}}', "{{pool}}"), refId="A")], 0, unit="percentunit"))
panels.append(ts("Stable load, p95 over 1h ↓", [dict(target(f'august:pool_used_bps_p95_1h{{{POOL},direction="download"}}', "{{pool}} p95"), refId="A"),
                                                dict(target(f'august:pool_used_bps_max_1h{{{POOL},direction="download"}}', "{{pool}} max"), refId="B")], 12))
y += 8

# ---------------------------------------------------------------- row 4
panels.append(row("4. Nodes",
    "'shared' means the node's capacity is accounted at its pool because the source data rates the "
    "location; 'unmeasured' means nothing is known. They are different states and are shown as such."))
panels.append(table("Nodes", [
    dict(target(f'august_capacity_mbps{{scope="node",{NODE},direction="download"}}', instant=True, fmt="table"), refId="A"),
    dict(target(f'august:node_free_ratio{{{NODE},direction="download"}}', instant=True, fmt="table"), refId="B"),
    dict(target(f'august:node_free_ratio{{{NODE},direction="upload"}}', instant=True, fmt="table"), refId="C"),
    # The absolute figure beside the percentage, because the alerts fire on this
    # one and a dashboard that shows only the ratio cannot explain a page.
    dict(target(f'august:alert_node_free_mbps{{{NODE},direction="download"}}', instant=True, fmt="table"), refId="N"),
    dict(target(f'august:alert_node_free_mbps{{{NODE},direction="upload"}}', instant=True, fmt="table"), refId="O"),
    dict(target(f'august:headroom_status_worst{{{NODE}}}', instant=True, fmt="table"), refId="D"),
    dict(target(f'august:node_health_status{{{NODE}}}', instant=True, fmt="table"), refId="E"),
    dict(target(f'august_node_enabled{{{NODE},{POOL}}}', instant=True, fmt="table"), refId="F"),
    dict(target(f'august_node_connected{{{NODE},{POOL}}}', instant=True, fmt="table"), refId="G"),
    dict(target(f'august_node_administratively_disabled{{{NODE},{POOL}}}', instant=True, fmt="table"), refId="H"),
    dict(target(f'august_node_active{{{NODE},{POOL}}}', instant=True, fmt="table"), refId="I"),
    dict(target(f'august_node_identity_mapped{{{NODE}}}', instant=True, fmt="table"), refId="J"),
    dict(target(f'sum by (node) (august_capacity_shared_pool{{name=~"$node"}})', instant=True, fmt="table"), refId="K"),
    dict(target(f'august:node_meta{{{NODE},{POOL},country=~"$country"}}', instant=True, fmt="table"), refId="M"),
    dict(target(f'sum by (node) (august_capacity_unrated{{scope="node",{NODE}}})', instant=True, fmt="table"), refId="L"),
], h=12, overrides=status_override))
y += 12
panels.append(ts("Node usage ↓", [dict(target(f'august:node_used_bps{{{NODE},direction="download"}}', "{{node}}"), refId="A")], 0))
panels.append(ts("Node usage ↑", [dict(target(f'august:node_used_bps{{{NODE},direction="upload"}}', "{{node}}"), refId="A")], 12))
y += 8

# ---------------------------------------------------------------- row 5
panels.append(row("5. Bridges — counted by source outbound tag, never added to a pool or to the service figure"))
panels.append(table("Bridges", [
    dict(target(f'august_bridge_info{{{BRIDGE}}}', instant=True, fmt="table"), refId="A"),
    dict(target(f'august_capacity_mbps{{scope="bridge",{BRIDGE},direction="download"}}', instant=True, fmt="table"), refId="B"),
    dict(target(f'august:bridge_used_bps{{{BRIDGE},direction="download"}} / 1e6', instant=True, fmt="table"), refId="C"),
    dict(target(f'august:bridge_free_ratio{{{BRIDGE},direction="download"}}', instant=True, fmt="table"), refId="D"),
    dict(target(f'august:bridge_free_ratio{{{BRIDGE},direction="upload"}}', instant=True, fmt="table"), refId="E"),
    dict(target(f'august:alert_bridge_free_mbps{{{BRIDGE},direction="download"}}', instant=True, fmt="table"), refId="I"),
    dict(target(f'august:alert_bridge_free_mbps{{{BRIDGE},direction="upload"}}', instant=True, fmt="table"), refId="J"),
    dict(target(f'august:headroom_status_worst{{{BRIDGE}}}', instant=True, fmt="table"), refId="F"),
    dict(target(f'august_bridge_enabled{{{BRIDGE}}}', instant=True, fmt="table"), refId="G"),
    dict(target(f'august:bridge_metric_missing{{{BRIDGE}}}', instant=True, fmt="table"), refId="H"),
], h=8, overrides=status_override))
y += 8
panels.append(ts("Bridge usage ↓", [dict(target(f'august:bridge_used_bps{{{BRIDGE},direction="download"}}', "{{name}}"), refId="A")], 0))
panels.append(ts("Bridge usage ↑", [dict(target(f'august:bridge_used_bps{{{BRIDGE},direction="upload"}}', "{{name}}"), refId="A")], 12))
y += 8

# ---------------------------------------------------------------- row 6
panels.append(row("6. Connections",
    "Established VPN inbound sockets, not users: one user can hold several and a multiplexed "
    "client can hold one for many streams. The panel's own online-user count is shown beside them."))
panels.append(table("Connections", [
    dict(target(f'august:node_sessions{{{NODE}}}', instant=True, fmt="table"), refId="A"),
    dict(target(f'august_node_session_limit{{{NODE},{POOL}}}', instant=True, fmt="table"), refId="B"),
    dict(target(f'august:node_session_ratio{{{NODE}}}', instant=True, fmt="table"), refId="C"),
    dict(target(f'august:node_online_users{{{NODE}}}', instant=True, fmt="table"), refId="D"),
    dict(target(f'august:node_tcp_passive_opens_per_second{{{NODE}}}', instant=True, fmt="table"), refId="E"),
    dict(target(f'august_node_vpn_ports_declared{{{NODE}}}', instant=True, fmt="table"), refId="F"),
], h=8, overrides=status_override))
y += 8
panels.append(ts("VPN inbound established sockets", [dict(target(f'august_node_vpn_established_sockets{{{NODE}}}', "{{node}} :{{port}} {{family}}"), refId="A")], 0, unit="none"))
panels.append(ts("Host TCP passive opens per second", [dict(target(f'august:node_tcp_passive_opens_per_second{{{NODE}}}', "{{node}}"), refId="A")], 12, unit="ops"))
y += 8

# ---------------------------------------------------------------- row 7
panels.append(row("7. Quotas", "Nodes with no quota are shown as unlimited and raise no alert."))
panels.append(table("Quotas", [
    dict(target(f'august_node_traffic_used_bytes{{{NODE},{POOL}}}', instant=True, fmt="table"), refId="A"),
    dict(target(f'august_node_traffic_limit_bytes{{{NODE},{POOL}}}', instant=True, fmt="table"), refId="B"),
    dict(target(f'august:node_quota_ratio{{{NODE},{POOL}}}', instant=True, fmt="table"), refId="C"),
], h=8, overrides=status_override + [
    {"matcher": {"id": "byRegexp", "options": ".*bytes.*"},
     "properties": [{"id": "unit", "value": "bytes"}]},
    {"matcher": {"id": "byName", "options": "Value #B"},
     "properties": [{"id": "mappings", "value": [{"type": "value", "options": {"0": {"text": "unlimited", "index": 0}}}]}]},
]))
y += 8

# ---------------------------------------------------------------- row 8
panels.append(row("8. Scaling and recommendation",
    "Priority: 1 RED bridge, 2 RED pool, 3 YELLOW bridge, 4 YELLOW pool, 5 overloaded active node. "
    "Ties: lowest free %, then lowest absolute free capacity, then highest growth. Sort by rank "
    "ascending - the top row is the recommendation."))
panels.append(table("Next node recommendation", [
    dict(target('sort(august:scaling_rank)', instant=True, fmt="table"), refId="A"),
    dict(target('august:scaling_priority > 0', instant=True, fmt="table"), refId="B"),
    dict(target('august:scaling_free_ratio', instant=True, fmt="table"), refId="C"),
    dict(target('august:scaling_free_mbps', instant=True, fmt="table"), refId="D"),
    dict(target('august:pool_growth_ratio', instant=True, fmt="table"), refId="E"),
], h=9, overrides=status_override))
y += 9
panels.append(ts("Time to RED at the current trend", [dict(target(f'august:pool_seconds_to_red{{{POOL},direction="download"}}', "{{pool}}"), refId="A")], 0, unit="s",
                 desc="Empty where the history is too short, the trend is flat or falling, or the series reset. "
                      "A forecast is not published unless it means something."))
panels.append(ts("Growth: short window over long window", [dict(target(f'august:pool_growth_ratio{{{POOL},direction="download"}}', "{{pool}}"), refId="A")], 12, unit="none",
                 desc="Gated on having enough samples and a non-zero baseline."))
y += 8

# ---------------------------------------------------------------- row 9
panels.append(row("9. Infrastructure and data quality",
    "Everything above is only as good as this row is quiet."))
panels.append(stat("Inventory valid", "august_capacity_inventory_valid", "none", 0, mappings=BOOL_MAP))
panels.append(stat("Serving last-good inventory", "august_capacity_inventory_last_good_in_use", "none", 4, mappings=BOOL_MAP))
panels.append(stat("Panel reachable", "august_capacity_panel_reachable", "none", 8, mappings=BOOL_MAP))
panels.append(stat("Panel state age", "august_capacity_panel_state_age_seconds", "s", 12))
panels.append(stat("Panel state stale", "august_capacity_panel_state_stale", "none", 16, mappings=BOOL_MAP))
panels.append(stat("Topology drift", "sum(august_topology_drift)", "none", 20))
y += 4
panels.append(table("Data quality", [
    dict(target('august:required_series_present', instant=True, fmt="table"), refId="A"),
    dict(target('august_node_identity_problems', instant=True, fmt="table"), refId="B"),
    dict(target('august_topology_drift', instant=True, fmt="table"), refId="C"),
    dict(target('august_semaphore_response_contract_ok', instant=True, fmt="table"), refId="D"),
    dict(target('august_semaphore_history_truncated', instant=True, fmt="table"), refId="E"),
], h=8, overrides=status_override))
y += 8
panels.append(ts("CPU busy", [dict(target(f'100 - (avg by (node) (rate(node_cpu_seconds_total{{mode="idle",{NODE}}}[5m])) * 100)', "{{node}}"), refId="A")], 0, w=8, unit="percent"))
panels.append(ts("Memory used", [dict(target(f'1 - (node_memory_MemAvailable_bytes{{{NODE}}} / node_memory_MemTotal_bytes{{{NODE}}})', "{{node}}"), refId="A")], 8, w=8, unit="percentunit"))
panels.append(ts("Load average, 5m", [dict(target(f'node_load5{{{NODE}}}', "{{node}}"), refId="A")], 16, w=8, unit="none"))
y += 8
panels.append(ts("Network errors and drops per second", [
    dict(target(f'sum by (node) (rate(node_network_receive_errs_total{{{NODE}}}[5m]) + rate(node_network_transmit_errs_total{{{NODE}}}[5m]))', "{{node}} errors"), refId="A"),
    dict(target(f'sum by (node) (rate(node_network_receive_drop_total{{{NODE}}}[5m]) + rate(node_network_transmit_drop_total{{{NODE}}}[5m]))', "{{node}} drops"), refId="B"),
], 0, w=8, unit="ops"))
panels.append(ts("TCP retransmit rate", [dict(target(f'rate(node_netstat_Tcp_RetransSegs{{{NODE}}}[5m]) / clamp_min(rate(node_netstat_Tcp_OutSegs{{{NODE}}}[5m]), 1)', "{{node}}"), refId="A")], 8, w=8, unit="percentunit",
                 desc="Retransmitted segments over sent segments."))
panels.append(ts("User port reachability and TCP connect latency", [
    dict(target(f'probe_success{{{NODE}}}', "{{node}} :{{port}} up"), refId="A"),
    dict(target(f'probe_duration_seconds{{{NODE}}}', "{{node}} :{{port}} latency"), refId="B"),
], 16, w=8, unit="s",
   desc="Availability from the monitoring host, on the ports users connect to. Packet loss is not "
        "measured: nothing here sends ICMP, and inventing a loss figure from a TCP connect would be "
        "a number with no source."))
y += 8
panels.append(table("Provisioning — Semaphore job durations", [
    dict(target('august_semaphore_last_success_timestamp_seconds', instant=True, fmt="table"), refId="A"),
    dict(target('august_semaphore_last_success_duration_seconds', instant=True, fmt="table"), refId="B"),
    dict(target('august_semaphore_task_duration_seconds{quantile="0.5"}', instant=True, fmt="table"), refId="C"),
    dict(target('august_semaphore_task_duration_seconds{quantile="0.95"}', instant=True, fmt="table"), refId="D"),
    dict(target('august_semaphore_task_duration_samples', instant=True, fmt="table"), refId="E"),
    dict(target('august_semaphore_tasks_read', instant=True, fmt="table"), refId="F"),
], h=8))
y += 8
panels.append(table("First observed connected", [
    dict(target(f'august_node_first_observed_connected_timestamp_seconds{{{NODE},{POOL}}}', instant=True, fmt="table"), refId="A"),
    dict(target(f'august_node_created_timestamp_seconds{{{NODE},{POOL}}}', instant=True, fmt="table"), refId="B"),
], h=8,
   desc="First OBSERVED connection: when this exporter first saw the node connected - not when it first came up. Nothing in the "
        "panel records that, and this series starts when the exporter does, so the gap between "
        "creation and service cannot be claimed from it."))

def variable(name, query, label, multi=True, all_value=".*"):
    return {"name": name, "label": label, "type": "query", "datasource": DS,
            "refresh": 1, "query": query, "includeAll": True, "multi": multi,
            "allValue": all_value, "current": {"selected": True, "text": ["All"], "value": ["$__all"]},
            "sort": 1}

dashboard = {
    "uid": "august-capacity",
    "title": "August VPN — capacity and health",
    "description": (
        "Service capacity and usage count each user gigabit once; the physical figures count every "
        "leg and are diagnostic. Bridges are measured by the source node's outbound tag and never "
        "added to a pool or to the service figure. A pool rated as a location shows 'aggregate' and "
        "its free percentage is an upper bound."
    ),
    "tags": ["august", "capacity"],
    "timezone": "utc",
    "schemaVersion": 39,
    "version": 2,
    "editable": False,
    "refresh": "1m",
    "time": {"from": "now-6h", "to": "now"},
    "panels": panels,
    "templating": {"list": [
        variable("environment", "label_values(august_service_capacity_mbps, fleet)", "Environment", multi=False),
        variable("pool", "label_values(august_pool_members, pool)", "Pool"),
        variable("country", 'label_values(august:node_meta, country)', "Country / location"),
        variable("node", 'label_values(august:node_meta{pool=~"$pool",country=~"$country"}, node)', "Node"),
        variable("bridge", "label_values(august_bridge_info, name)", "Bridge"),
    ]},
}
out = pathlib.Path(__file__).resolve().parents[0] / "grafana/august-capacity.json"
out.write_text(json.dumps(dashboard, indent=2, ensure_ascii=False) + "\n")
print("panels:", len(panels), "rows:", sum(1 for p in panels if p["type"] == "row"), "bytes:", out.stat().st_size)
