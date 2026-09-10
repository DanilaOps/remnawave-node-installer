# Capacity inventory

`capacity.yml` is the single statement of what this fleet is rated to carry. It
is versioned because a capacity number is a claim, and a claim that decides
when somebody buys another server has to be reviewable in a diff: who wrote it,
when, from which measurement.

It is not installed by Ansible: the monitoring server is set up by hand,
and this file is copied to it - see `monitoring/README.ru.md`. It is
validated by `monitoring/validate_capacity.py`, read by
`monitoring/capacity_exporter.py`, and tested by
`monitoring/tests/test_capacity_model.py`. All three import the same module, so
the rules described here, the rules CI enforces and the rules the running
exporter applies are one set of rules.

```bash
python3 monitoring/validate_capacity.py monitoring/capacity/capacity.yml
```

## What must never be in this file

No tokens, no passwords, no SSH keys, no UUIDs, no public IP addresses. A node
is named the way the fleet names it — `TR-01` — and everything that maps that
name to a machine lives in the Semaphore inventory and in `/etc/remnawave`. The
validator refuses all five shapes, so this is enforced and not merely asked for.

There is also no notion of planned, potential or post-expansion capacity. The
expansion scenario in `network-capacity.md` is a planning document; this file
states what a link is rated at today, because this is what alerts fire on.

## Structure

```yaml
version: 1
defaults: {session_limit: 0, quota_bytes: 0}
nodes:   {<NAME>: {pool, enabled, capacity, measurement, evidence?}}
pools:   {<NAME>: {strategy, members, capacity?, measurement?, members_unknown_reason?}}
bridges: {<NAME>: {source_node, destination_node, outbound_tag, enabled, capacity, measurement, evidence?}}
```

### capacity

Stated separately for `download` and `upload`, always. The two directions are
carried apart through every recording rule and every panel, because they fail
apart: a link can be full outbound and idle inbound.

Direction follows the user, not the wire. `download` is traffic towards the
user; on a bridge that means destination → RU.

Each direction has a `source`, and the source decides what is published:

| `source` | Needs | Published | Meaning |
|---|---|---|---|
| `measured` | `mbps`, `measured_at` | `mbps` | Somebody ran a test and wrote down the date. |
| `declared` | `mbps` | `mbps` | The owner's rating table. |
| `range` | `mbps_min`, `mbps_max`, `policy` | see below | The source data gives a range. |
| `shared_pool` | nothing | nothing on the node | The rating exists, but only for the location as a whole. It lives on the pool. |
| `unmeasured` | nothing | nothing | No rating at all. Excluded from every total, and reported. |

`shared_pool` and `unmeasured` are **not** the same state and must not be
collapsed into one. `shared_pool` says "we know what this location carries, we
do not know this machine's share of it"; `unmeasured` says "we know nothing".
The exporter publishes `august_capacity_shared_pool` for the first and
`august_capacity_unrated` for the second, and the Nodes table shows them as
different words.

### attribution

Every rated entry also carries `attribution`:

* `per_node` - the number belongs to this machine. Losing it costs exactly
  that much.
* `aggregate` - the number belongs to a location. How much of it any single
  machine carries is not known, and the system must not pretend otherwise.

An `aggregate` pool publishes `august_pool_capacity_aggregate 1` and
`august_pool_capacity_certain 0`, the free percentage is presented as an upper
bound, and `AugustPoolCapacityUncertain` fires when the pool is not at full
membership. **Inventing per-node values to make the arithmetic tidy is
forbidden**, and `capacity.shared_pool_attribution` is a validation blocker for
exactly that mistake.

`DE` is the case this exists for: the owner's table rates Germany at
2600 Mbit/s as one number, so the rating sits on the pool with
`attribution: aggregate` and `DE-01`, `DE-02` and `DE-03` each carry
`source: shared_pool` and no figure of their own.

### serves_users

A pool that exists only to carry other pools' traffic sets
`serves_users: false`. It is monitored and alerted on exactly like any other
pool, and it is left out of `august_service_capacity_mbps`, because counting
transit as service capacity is the same double count a bridge row would be.
`FL` is that pool today.

A `range` needs an explicit `policy`, and a range without one is a validation
blocker rather than a default:

* `conservative` publishes `mbps_min` and marks the series
  `conservative="true"`, so a dashboard can say the figure is a floor.
* `blocker` publishes nothing and fails validation, which is what to use when a
  floor is not good enough to plan against.

Writing an exact `mbps` beside a range is refused (`capacity.invented_point`).
That is the whole point of the field: **FR 5–6 Gbit/s and TR 3–4 Gbit/s must
never quietly become 5500 and 3500.** They are published today as 5000 and 3000
with `policy: conservative`; narrowing them needs a measurement, not an edit.

### measurement

Which counter this rating is meant to be compared against.

```yaml
measurement:
  kind: inbound | outbound
  node: TR-01                     # whose counter
  tag: RU01-40913C-DE-BRIDGE      # outbound only, and mandatory there
```

* `kind: inbound` is a node's own traffic, summed over every tag. It is *all*
  the traffic physically crossing that machine, including the share that
  arrived over a bridge — the destination end of a bridge really is carrying
  those bytes, and its utilisation has to show them.
* `kind: outbound` is one tag leaving one node. A bridge is measured this way
  and no other way, because the source node's outbound counter for the bridge's
  own tag is the only counter that separates bridge traffic from the user
  traffic of the same machine. An outbound measurement without a tag is a
  blocker: a shared `DIRECT` tag mixes the two and cannot be used.

`tag` on an inbound measurement is refused, `kind: outbound` on a node is
refused, `kind: inbound` on a bridge is refused, and two bridges sharing one
tag is refused.

### pools

A pool is a group of interchangeable nodes. Normally its capacity is the sum of
its active members' and it states none of its own.

A pool states its own `capacity` **only when the source data cannot honestly be
split between its machines** — the owner's table rates FR and TR as locations,
not as individual servers. Then:

* naming the members is fine and wanted — the dashboard and the pool tables
  need them, and a location whose machines are known but unmeasured is the
  ordinary case;
* what is refused is a pool figure standing next to a **rated** member: then
  the same gigabits are in both rows and the pool total carries them twice
  (`pools.capacity_and_members`). Rate the members and drop the pool figure, or
  leave the members unrated;
* an empty `members` list needs `members_unknown_reason`, so a gap is a
  recorded gap and not a typo;
* with members named, the pool's own figure follows them: every member down
  means no active capacity, exactly as for a pool rated member by member. With
  no members named there is nothing to follow, the figure stands, and
  `AugustPoolCapacityUnattributable` says so out loud.

`FR` and `TR` are that case today: one named machine each (`FR-01`, `TR-01`),
neither measured on its own, and the owner's range kept at the pool.
`RU_AUTO` is the other end of it — `RU-05` and `RU-06` are named, and neither
they nor the pool carry any rating at all, so the pool is monitored and alerted
on while contributing nothing to a capacity total.

### Why a bridge row is not added to the pool

A bridge's bytes are already inside the inbound counters of the nodes at both
of its ends. Adding the bridge row to a pool or fleet total would count the
same gigabits a second time and make the fleet look bigger than it is, which is
the direction of error that gets noticed only when something is full.

So: `august_service_capacity_mbps` is summed over pools that serve users, never
over `august_capacity_mbps`, and bridge rows carry `scope="bridge"` and are
reported on their own. The figure that does include every leg is
`august_physical_capacity_mbps`, and it is labelled a diagnostic everywhere it
appears. `monitoring/tests/test_capacity_model.py::DoubleCountingTests` fails the
build if that ever changes, and `ansible/tests/test_service_accounting.sh`
proves the same contract against the real PromQL engine with a synthetic
1 Gbit/s flow through a bridge.

The reverse also holds: a node that exists only as a bridge destination is not
given the bridge's rating as if it were independent user capacity. `FL-01`,
`LV-01` and `US-01` are `unmeasured` for exactly that reason, and the fleet
total therefore comes out at **24 100 Mbit/s**, which is the lower bound of the
owner's table — a number the tests assert against.

### enabled, and what "active" means

`enabled: false` takes a node out by hand. Separately, the exporter reads the
panel on every scrape and drops a node that is `isDisabled`, not `isConnected`,
or unknown to the panel. Active capacity is what is left; it shrinks when a node
goes and comes back when it returns, with no edit to this file.

### evidence

Optional, and never published as capacity:

```yaml
evidence:
  - {kind: iperf3, direction: upload, mbps: 4739, at: "2026-08-31", note: "..."}
```

A run more than 1.2× the published rating raises
`capacity.evidence_above_declared` — a warning, not a blocker. It means the
rating and the measurement disagree and a human should decide which is wrong,
rather than one afternoon's test silently becoming the number that alerts fire
on.

## Adding a node

1. Add the machine to the Semaphore inventory as one line (`ansible/README.md`)
   and install it the ordinary way; with `node_monitoring_enabled: true` the
   node comes up already exporting.
2. Add it here, under `nodes`, with its pool and its rating.
3. Add it to that pool's `members`.
4. Run `python3 monitoring/validate_capacity.py monitoring/capacity/capacity.yml`.
5. Copy this file to `/etc/august-monitoring/capacity.yml` on the monitoring
   server and restart the capacity exporter, and add the node's address to
   `/etc/prometheus/targets/nodes.json` (no restart needed there - `file_sd` is
   re-read on its own). See `monitoring/README.ru.md`.

The name used here, the name in the Semaphore inventory and the `node` label in
`nodes.json` must be the same string. Where they diverge, the exporter says so:
`august_topology_drift`.

If the rating is not known yet, say `source: unmeasured`. The node will be
monitored, and its capacity will be reported as unrated rather than guessed.
