# HA Irrigation Controller

[![Validate](https://github.com/ic3rus/ha-irrigation-controller/actions/workflows/validate.yml/badge.svg)](https://github.com/ic3rus/ha-irrigation-controller/actions/workflows/validate.yml)
[![Test](https://github.com/ic3rus/ha-irrigation-controller/actions/workflows/test.yml/badge.svg)](https://github.com/ic3rus/ha-irrigation-controller/actions/workflows/test.yml)
[![Lint](https://github.com/ic3rus/ha-irrigation-controller/actions/workflows/lint.yml/badge.svg)](https://github.com/ic3rus/ha-irrigation-controller/actions/workflows/lint.yml)
[![Card](https://github.com/ic3rus/ha-irrigation-controller/actions/workflows/card.yml/badge.svg)](https://github.com/ic3rus/ha-irrigation-controller/actions/workflows/card.yml)

A Home Assistant custom integration for deterministic irrigation scheduling — a
hass-free sequencer engine driving your valves, plus a bundled Lovelace timeline
card (`ha-irrigation-timeline-card`) delivered in the same install.

**Requires Home Assistant 2026.9.0 or newer** (setup fails with a clear error on
older versions).

## Installation (HACS custom repository)

1. In HACS, open **⋮ → Custom repositories**.
2. Add `https://github.com/ic3rus/ha-irrigation-controller` with category
   **Integration**.
3. Install **HA Irrigation Controller**, then restart Home Assistant.
4. Go to **Settings → Devices & services → Helpers** and choose
   **Create helper → HA Irrigation Controller**.

> This integration declares `integration_type: helper`, so Home Assistant lists
> it under **Helpers** — it does *not* appear in the "Add integration" dialog.

Only one controller can be configured (`single_config_entry`).

The built card ships inside `custom_components/`, so a HACS install delivers it
in the same payload — no separate card install or download. The integration
serves the bundle and registers the dashboard resource itself; see
[Dashboard card](#dashboard-card).

## Dashboard card

The integration bundles a Lovelace card, `ha-irrigation-timeline-card`, that
shows **today's watering plan**: one row per cycle — morning and evening when
the morning cycle is enabled, evening alone otherwise — each row with its own
time axis running from the cycle's start to its end, and one segment per zone,
proportional to its duration and labelled in plain text with the zone name and
its planned start–end in your Home Assistant time format. Before a cycle runs,
the rows come from the configured plan (base durations); once a cycle is
running, its quoted windows are drawn instead. Live progress, the 7-day
history, the health indicator and the visual editor arrive in later stories.

The card reads the [state view](#state-view-and-websocket-api) over the
`state_subscribe` WebSocket command and nothing else — no entity states, no
bus events — so it works for non-admin household members too, and it
re-renders only when a new document is pushed, not on every state change in
the house.

### Adding the card

Pick **HA Irrigation Timeline Card** in the dashboard card picker, or add it in
YAML:

```yaml
type: custom:ha-irrigation-timeline-card
title: Garden # optional; defaults to "Irrigation"
entry_id: 01J... # optional; only needed with several controllers
```

With no options the card finds the single controller by itself. If no
controller is set up, or it is reloading, the card says so in its body and
retries on its own.

### The card resource

The bundle is served by the integration at
`/ha_irrigation_controller/ha-irrigation-timeline-card.js`. When your dashboard
resources are managed in **storage mode** (the default — the list under
**Settings → Dashboards → ⋮ → Resources**, shown only with **Advanced mode**
enabled in your user profile), the integration registers the resource itself
at startup: one `module` resource with that URL plus
`?v=<integration version>`, so an upgrade invalidates the browser's cached
copy. On upgrade the existing entry is updated in place — nothing is duplicated
and there is nothing to do.

**YAML mode fallback.** When resources are managed in YAML (`lovelace:` with
`mode: yaml` or `resource_mode: yaml`), Home Assistant does not let an
integration add resources. The integration logs one line pointing here; add the
resource yourself:

```yaml
lovelace:
  mode: yaml
  resources:
    - url: /ha_irrigation_controller/ha-irrigation-timeline-card.js?v=0.1.0
      type: module
```

Bump `v=` to the new version after each upgrade (the version is in
`custom_components/ha_irrigation_controller/manifest.json`); browsers may keep
the old bundle otherwise. Neither case stops the integration from starting —
the bundle is served regardless, and a Home Assistant without Lovelace at all
simply gets no resource (one debug-level line).

## Services

Four actions are available to automations, scripts and voice assistants. They
are registered at component setup, so they stay callable (and editable in an
automation) even while the integration is reloading — a call made while nothing
is loaded fails with a clear message rather than disappearing.

### `ha_irrigation_controller.cancel_cycle`

No fields. Stops the cycle running right now: the open valve is closed, then
the pump, and the run is recorded as **cancelled**. This is the *only* thing
that stops a running cycle. Zones the cycle never reached are recorded as
having watered zero seconds — and, like the zone that was cut short, they
carry the shortfall forward (see [Water debt](#water-debt)).

Calling it with nothing running is an error, not a silent no-op.

### `ha_irrigation_controller.run_now`

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `cycle` | `morning` \| `evening` | yes | Which daily cycle to run |

Starts a full cycle immediately. It is the scheduled cycle in every respect:
the pump starts first, the zones water one at a time in their configured
order, each actuation is verified, and the run is journalled and recorded in
history — where it is marked as a **manual** run so it can be told apart from
the scheduled cycle of the same kind on the same day.

`cycle` is required and has no default. Each zone stores a separate duration
for the morning and the evening cycle, so naming the cycle is what decides how
long every zone waters; the controller never guesses one from the time of day.

**It runs with the season off.** Ending the season suspends *scheduling*;
watering once on demand is an explicit decision, so it is not gated. Turning
the season off while a run-now cycle is in progress does not stop it either.

**It runs the morning cycle even when the morning cycle is turned off.**
"Run a morning cycle" suppresses the daily 07:00 *start*, not the cycle
itself, so `cycle: morning` on an evening-only controller waters every zone on
its morning durations. Asking for it by name is the decision.

It is refused — with a translated error and nothing started — when a cycle is
already running *or* waiting to start (two zones must never water at once; use
`cancel_cycle` first if you mean to replace it), and when the controller has no
zones configured.

**Credits the day.** A run-now that **completes every zone in full** credits
its irrigation day, and the *next* scheduled cycle of that same day is then
**waived**: no run is created, nothing is commanded, no anomaly is raised, and
history records the cycle as `waived` with `waived_by` naming the run-now.
The credit is consumed by that one decision, so the day's *other* scheduled
cycle waters normally — a morning run-now waives the morning cycle if it has
not started yet, or the evening one if it has. The credit does not care which
cycle the run-now was: an evening run-now completed before the morning cycle
waives the morning cycle. A scheduled cycle that falls
due *while* the run-now is watering is queued as usual and waived when the
run-now completes. The credit belongs to the calendar day of the cycle's
configured start; if no scheduled cycle asks for it that day, it is simply
discarded by the first one that does on a later day, and that cycle waters.

A run-now that was **cancelled**, or that completed with a zone that failed
to open, or that otherwise left any zone short of its quoted duration,
credits **nothing** — doubt resolves toward watering — and its under-watered
zones carry their shortfall forward (see [Water debt](#water-debt)). A
second completed run-now on the same day refreshes the credit rather than
adding one. The credit lives in the integration's journal, so it survives a
reload and a Home Assistant restart, and the **Cycle status** sensor shows
it as a `day_credit` attribute (the credited day, or none).

### `ha_irrigation_controller.set_season`

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `enabled` | boolean | yes | `true` resumes scheduling, `false` suspends it |

The one-action end of season. With the season off, a daily start creates no
cycle, queues nothing and raises **no anomaly** — a suspended season is a
legitimate reason not to water, not a fault. Any cycle already queued behind a
running one is dropped.

**A cycle already running is not stopped** — it completes normally, pump-off
included. Use `cancel_cycle` for that.

The same state is exposed as a switch entity on the controller device, so
`switch.turn_on` / `switch.turn_off` on it does exactly the same thing. The
setting is stored in the integration's own journal, not in its configuration,
so it survives a restart and a reload without triggering one.

### `ha_irrigation_controller.set_zone_duration`

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `zone_id` | string | yes | The zone's config subentry id |
| `cycle` | `morning` \| `evening` | yes | Which daily cycle the duration applies to |
| `duration` | integer, **minutes** (1–120) | yes | How long that zone waters in that cycle |

`zone_id` is the zone's *identifier*, not its name. If you pass an unknown one,
the error message lists every configured zone as `id (name)` so you can copy
the right one.

The change applies without restarting Home Assistant. The call is rejected —
with a translated error and no write — when the duration is outside 1–120
minutes, or when it would make the morning and evening cycles overlap.

Like every other configuration change, it follows the rules below when a
cycle is running. The action deliberately does *not* refuse the call, because
durations are meant to be editable at any moment.

## Water debt

A zone that waters less than planned — its valve never confirmed open, the
cycle was cancelled before or during its slot, a late catch-up ran every
boundary at once — does not simply lose that water. The shortfall is recorded
as a per-zone **deficit** and added to that zone's *next* cycle, whichever
kind runs next: a zone that is owed 5 minutes waters its configured duration
plus 5 minutes, and the zones after it start correspondingly later. Water debt
is a *floor*, never a ceiling: every doubtful case (a valve that timed out but
may have opened, a cycle that ran with no dwell time) is booked as a deficit
and watered again.

An extended cycle can still be running when the other cycle's configured
start arrives. That cycle is then queued and starts the moment the running one
finishes — delayed, never overlapped and never skipped. The overlap check the
configuration forms apply to your start times looks at the configured
durations only and ignores debt.

The deficit is **capped at one full configured duration**, both when it is
recorded and when it is applied — so a zone never waters more than twice its
configured duration in one cycle, and repeated failures never snowball. A zone
that waters its full extended duration clears its debt. The debt of a zone
removed from the configuration is never applied again and is dropped when the
next cycle completes. The ledger lives in the integration's own journal, so a
deficit survives a reload and a Home Assistant restart. When a zone is both
owed water and covered by [rain](#rain-credit), the rain credit is taken off
the configured duration first and the debt is added afterwards — so a zone in
debt always waters at least what it is owed, whatever the rainfall.

`run_now` uses the same arithmetic: a manual cycle applies, and then clears,
outstanding deficits exactly like a scheduled one — and, when it completes
with nothing left owed, it [credits the day](#ha_irrigation_controllerrun_now)
so the next scheduled cycle is waived. A waived cycle is never settled: a
deficit outstanding when a cycle is waived stays in the ledger and extends the
next cycle that actually runs.

Each zone's **Last watering duration** sensor carries two attributes:
`carried_deficit`, the seconds that were added to the run the sensor is
showing, and `pending_deficit`, the seconds the zone is owed according to the
ledger. The ledger only moves when a cycle completes (or is cancelled), so
while the run that is paying a debt is still in progress `pending_deficit`
keeps showing that debt; it drops once the run finishes.

## Rain credit

The controller reads the configured **rain sensor** — any sensor reporting an
accumulated precipitation total — once per cycle, when the cycle is quoted,
and turns the rain that has fallen since each zone's *previous cycle* into a
per-zone credit. What counts is the sensor's cumulative total: the value at the
zone's previous cycle is remembered as that zone's baseline, and the
difference to the current value is the rain credited to this cycle. A zone
that has no baseline yet — the first cycle after installation, or a newly
added zone — always waters in full and only banks the gauge reading for the
next cycle. Rain that falls *during* a cycle is not lost; it is counted for
the next one. Baselines live in the integration's journal and survive a
reload and a restart. Every zone advances its baseline at every cycle,
sheltered and factor-0 zones included, so switching a zone to exposed only
credits rain fallen since its last cycle.

Only zones marked **exposed to rain** are reduced; a sheltered zone waters its
full duration whatever the rainfall. The conversion is the zone's **rain
factor**, in minutes of watering per millimetre of rain (default 1 min/mm; 0
disables the reduction for that zone). The credit is rounded down to whole
seconds, and a zone's duration can be reduced all the way to **zero**: such a
zone is **skipped** — its valve is never commanded, no anomaly is raised and
no deficit is recorded, since real fallen water is the one safe reason to
water nothing. Credit beyond a zone's configured duration is not carried
over: heavy rain excuses one cycle, never two. When every zone of a cycle is
skipped, the pump is not started either; the cycle still completes and is
filed in history with each zone marked `skipped`.

Each zone's **Last watering duration** sensor carries a `rain_credit`
attribute: the seconds of credit applied to the run the sensor is showing (0
when none). A skipped zone shows `0` seconds watered with its credit next to
it.

If the rain sensor is missing, `unknown`, `unavailable`, reports something that
is not a finite non-negative number, or reports a unit the controller does not
understand, rain modulation is simply disabled for that cycle: every zone
waters its full duration and no anomaly is raised — however long the sensor
stays that way. Each cycle's history record carries the gauge reading it was
quoted from (`rain_total_mm`); `null` there means no rain sensor is
configured, or the gauge was doubtful for that cycle — as opposed to a reading
that simply showed no rain. Sensors
reporting in `mm`, `cm` and `in` are all read correctly.

The controller expects a **cumulative total that never resets** — the
lifetime or season total of the gauge. A *daily* (since-midnight) total works
but over-waters: the morning cycle compares the overnight total against
yesterday's whole-day baseline, so everything that fell between the evening
cycle and the next morning's cycle is usually lost — and under-credited
otherwise, whenever the overnight rain does not exceed the previous day's
total. Lost rain only ever means more watering, never less. A sensor total that
*decreases* (a gauge reset) credits nothing for that cycle and becomes the new
baseline, so rain falling after the reset is credited from the next cycle on.

**Changing the rain sensor** (picking another entity in the options, or a
rename of the current one) re-banks: baselines are remembered together with
the sensor they were read from, and a reading from a different sensor is never
compared with them — even if the new sensor's total is much higher. The first
cycle after the change waters in full and banks the new sensor's reading; the
cycle after that is reduced as usual. Turning the **season** on does the same:
the rain accumulated while the season was off is forgotten, the first cycle of
the season waters in full, and modulation resumes from the second. Turning
the season off and on again *during* the season also forgets the rain banked
since the last cycle, so the next cycle waters in full (fail-wet). Turning the
season off keeps the baselines.

**Late-arriving readings**: some gauges upload their measurements in batches
after a connectivity gap. Whatever the sensor's total is when a cycle is
quoted is what counts, so rain that reaches the sensor late — including rain
that fell before a cycle quoted during the gap — is credited to the *next*
cycle in full. A watering that already happened is never undone or re-filed:
its history record and the baselines it banked stand.

`run_now` is rain-reduced like any other cycle: it runs the *current* effective
durations. A run-now fired after rain that covers every zone completes at once
with every zone skipped, waters nothing and — having left no debt — still
[credits the day](#ha_irrigation_controllerrun_now).

## Configuration changes and running cycles

Every configuration change — the options form, adding, editing or deleting a
zone, `set_zone_duration` — is applied by reloading the integration. That
reload is **cycle-aware**:

- **Nothing running:** the reload happens at once, as before.
- **A cycle is running (or waiting for its start):** the reload waits. The
  running cycle finishes with the parameters it started with — a zone whose
  duration you shortened still waters its original time — and the integration
  reloads exactly once, however many edits you made meanwhile. Any cycle
  created before that reload lands (one queued behind the running one, or the
  other daily start firing meanwhile) already uses the new configuration, and
  is itself completed before the reload — so the reload fires once nothing is
  running any more, which can be the end of that next cycle.

While a reload is waiting, the **Cycle status** sensor carries the attribute
`config_change_pending: true`; it returns to `false` when the last running
cycle completes and the reload fires.

**Forced reloads are different.** Reloading the integration yourself (the
"Reload" menu entry, `homeassistant.reload_config_entry`), disabling it or
removing it while a cycle runs does not wait: the open valve is closed, then
the pump, and a `cycle_interrupted` anomaly is raised. When the integration
loads again — at once for a reload, whenever you re-enable it otherwise —
the cycle is recovered exactly as after a restart (see [Restarts and
recovery](#restarts-and-recovery)): on the same irrigation day it resumes,
the interrupted zone being re-run in full since a closed valve cannot prove
what it watered, and `cycle_interrupted` is superseded by `cycle_recovered`.

## Entity renames

The controller stores the *entity ids* of the pump, the rain, temperature and
humidity sensors and every zone's valve. Renaming one of them in the entity
registry (Settings → Devices & services → Entities) is followed
automatically: the stored id is rewritten and the change applies through the
normal cycle-aware reload. A valve renamed while its zone is watering is still
closed correctly under its new id.

Two limits:

- Only entities that have an entity-registry entry (a `unique_id`) fire rename
  events. An entity defined without one cannot be tracked; if you rename such
  an entity, reconfigure the controller or the zone by hand.
- A configured entity that is **removed** from the registry, or **disabled**,
  raises a `configured_entity_missing` anomaly naming the entity and its role.
  Nothing is skipped or unscheduled: the next cycle still commands it and
  raises the usual unconfirmed-actuation anomalies. The anomaly clears itself
  when the entity is re-created (enabled) under its stored id or re-enabled,
  and also when you point the role at another entity or delete the zone —
  the reconfiguration supersedes it (see [Anomalies](#anomalies)).

## Restarts and recovery

Every cycle transition is journaled, so a Home Assistant restart (or a crash)
mid-cycle loses seconds of progress, never minutes. A valve left open by a
crash stays open until Home Assistant is back: there is no reliable shutdown
hook, nothing is ever closed from a shutdown handler, and bounded
over-watering is accepted. Recovery is therefore automatic and runs **once, at
startup, before any timer is armed** — immediately on a reload, and once Home
Assistant has fully started on a boot, so the valve and pump switches have had
time to report a real state.

Recovery reconciles the journaled cycle against the *actual* switch states:

1. **Orphans are closed first.** Every valve of the journaled cycle, then the
   pump, that is not reported `off` is commanded off through the verified
   switch path; a switch that is `unknown` or `unavailable` counts as not
   off. An unconfirmed close raises the usual `valve_close_unconfirmed` /
   `pump_off_unconfirmed` anomaly.
2. **Same irrigation day → the cycle resumes**, on the durations it was
   quoted with: nothing is re-quoted, so a deficit or rain credit that
   appeared meanwhile applies to the *next* cycle. The zone that was watering
   is credited only what can be proven — found `on`, it watered continuously
   since it opened and finishes its remaining time (or is closed at once if
   that time is up); found `off` or unknown, it is re-run in full, at most one
   zone's worth of over-watering. The zones after it follow back to back. A
   cycle that had not started yet starts immediately. The season being off
   does not stop a cycle in progress from completing; a cycle that had not
   started under season off is closed instead.
3. **A later day → the cycle is closed** and recorded as `interrupted`: the
   zone that was watering is credited its span if its valve was found on and
   nothing otherwise, the zones never reached are credited nothing, and each
   shortfall is carried forward as [water debt](#water-debt), capped as usual.
4. **An unreadable journal** (a restored backup, a hand edit, schema drift)
   is never trusted: every configured valve and the pump are commanded off,
   nothing is booked, and the cycle is dropped.

Every recovery raises one `cycle_recovered` anomaly whose `outcome` is
`resumed`, `closed` or `discarded` — one Repairs issue, one push
notification, one bus event; a further recovery while that issue is still
unacknowledged refreshes the issue with the new outcome and fires the event
again but, like every anomaly, pushes nothing new — and the recovered
cycle's history record
carries `recovery: "resumed"` or `"closed"`. A `cycle_interrupted` issue left
by a forced reload is cleared by the recovery that supersedes it. A nominal
restart, with no cycle in progress, is silent: no command, no anomaly, no
notification, no journal write. The last completed cycle survives restarts
and reloads too, so the per-zone **Last watering duration** sensors keep
their values.

The one thing an otherwise idle restart *does* act on is a cycle that was
**queued** when the controller went down — deferred behind a cycle that was
running, or behind [manual control on site](#manual-control-on-site). Nothing
is ever going to pop that queue otherwise, so if the cycle still belongs to
today it is watered as soon as recovery finishes.

## Missed cycles and late re-runs

A scheduled cycle can fail to run without anything going wrong at the valve:
Home Assistant is down across the window, or the start falls inside the hour
the clocks skip forward on the spring daylight-saving change (02:30 simply does
not happen that day). Before, that left no trace at all — no run, no history
entry, no notification.

The **missed-cycle watchdog** closes that hole. It compares what the plan
*expected* of today against what the journal actually records, and it looks at
two moments: **at startup**, and **at the end of each cycle's watering
window**. Both are served by the same single timer everything else uses.

A cycle whose window has closed with nothing to show for it is:

1. recorded in history as **`missed`** — that record is also the promise that
   it happens **at most once**: the same cycle is never detected, notified or
   re-run a second time, on this day, after a reload or after a restart;
2. reported as one `missed_cycle` anomaly — one Repairs issue, one push
   notification, one bus event; a further miss while that issue is still
   unacknowledged refreshes the issue with the new cycle and fires the event
   again but, like every anomaly, pushes nothing new;
3. **watered immediately**, on the same irrigation day, with durations
   calculated fresh (carried [water debt](#water-debt) and rain reduction both
   apply). One late re-run per missed cycle, ever — never a backlog.

The exception is a miss noticed **after the day's later cycle has already
run**. Watering on top of it would be the runaway compensation the controller
is built to avoid, so the cycle is recorded instead, and its missed watering is
carried forward as water debt — capped, as always, at one base duration per
zone. The same applies when the day's morning *and* evening were both missed:
only the evening is watered, and the morning is booked. If a cycle happens to
be watering at the moment the miss is noticed, the record is still filed but
nothing is booked: that cycle's own accounting replaces the ledger when it
finishes, so the debt would be discarded rather than carried.

The lookback is **today only**. If Home Assistant was down for three days, the
day it comes back is made up; the days it slept through simply have no entry
in the 7-day history.

Nothing that is a legitimate reason not to water is ever flagged, and nothing
of that kind is ever re-run:

- the **season is off**, or the **morning cycle is disabled** in the controller
  options;
- the cycle is **already in history** in any state — completed, rain-skipped,
  [waived](#ha_irrigation_controllerrun_now), cancelled, or recovered after an
  interruption;
- the cycle is **running or waiting** (deferred behind another cycle): it is
  late, not lost;
- a completed **run now** has already credited the irrigation day.

A nominal day is therefore completely silent: both window ends come and go
with no record, no notification and not even a journal write.

The watchdog only ever makes up a window that closed **while the controller
existed**. The first time it is set up, the moment of setup is recorded, and
every window that had already closed by then is simply not its business — so
configuring the controller at 16:00 never starts a surprise watering for the
07:00 cycle it was never there for. The same stamp is recorded once when you
upgrade to this version, so upgrading mid-afternoon makes nothing up either.
From the next window end onwards everything is watched normally.

Two things that do still get made up, because the journal records neither
change: **enabling the morning cycle** part-way through a day, and **turning
the season back on** part-way through a day. In both cases that day's already
passed windows have no record and count as missed. It does not happen the
instant you flip the switch — nothing is checked until the next window end —
so turning the season on after the day's last window makes nothing up at all,
while turning it on at noon makes the morning up when the evening window
closes.

## Manual control on site

Sometimes you just want to open a valve yourself — to flush a line, to check a
sprinkler head, to water a new plant. The controller does not fight you.

Every switch it governs — the pump and each zone's valve — is watched. A change
that **the integration did not command** is read as manual control, and while
at least one governed switch is being held **open** by hand the scheduler
**pauses**:

- a scheduled start falling inside the pause is **queued, not skipped** — it is
  late, not lost, and the [missed-cycle watchdog](#missed-cycles-and-late-re-runs)
  sees it as still due rather than missed;
- a cycle that had been scheduled but had not commanded anything yet is put
  back in that same queue;
- nothing is commanded and nothing is notified *while the pause lasts*. A
  pause is normal operation, not a fault: up to the
  [safety timeout](#the-safety-timeout) it raises no repair, sends no
  notification and fires no event.

When the **last** hand-opened switch goes off again — by your hand, by the
controller's own next command, or because its
[safety timeout](#the-safety-timeout) ran out — the scheduler resumes:
anything queued for
**today** is watered immediately, on freshly calculated durations (carried
[water debt](#water-debt) and [rain credit](#rain-credit) both apply);
anything queued for an earlier day is dropped. Two valves opened by hand need
two closes: the pause holds while any one of them is open.

A **cycle already running is never interrupted**. It waters its zones to the
end on the schedule it started with, and a valve you close by hand mid-slot is
not re-opened — the controller only commands at slot boundaries. What the pause
stops is the *next* cycle starting, not the current one finishing.

**Your own commands are never refused.** The pause holds back the *scheduler*,
not you: [`run_now`](#ha_irrigation_controllerrun_now) starts a cycle while it
is in force — that is you asking, not the schedule guessing —
[`cancel_cycle`](#ha_irrigation_controllercancel_cycle) still stops one, and the
season switch still works.

The **Cycle status** sensor carries the whole thing as one attribute,
`manual_override`: `true` while the scheduler is paused, `false` otherwise.
There is no extra entity to add to a dashboard.

Two limits worth knowing:

- **Detection is best-effort.** Home Assistant cannot guarantee that every
  state change can be attributed, and the controller deliberately errs towards
  "not manual": a switch appearing at startup, one going `unavailable` and
  back, and any change the controller can tie to one of its own commands are
  all ignored. The failure direction is to miss a manual act, never to invent a
  pause that stops watering.
- **The pause is lost across a restart or a reload.** It lives in memory
  only, so restarting Home Assistant — or editing the controller's options,
  which reloads the integration — ends the pause and, with it, the safety
  timeout below. The queued cycle survives: it waters once the controller is
  back and nothing else is in progress. See
  [The safety timeout](#the-safety-timeout) for what that means for a valve
  still physically open.

Two consequences of that last point are worth knowing, because in both the
pause ends — or its queued cycle disappears — without you doing anything:

- **A held-open switch that goes `unavailable`, or is removed, releases it.**
  Once the controller can no longer see the switch open, it stops holding the
  scheduler for it: the alternative is a pause that never ends, with no
  watering and nothing said. If the switch comes back still open, flipping it
  again is what pauses the scheduler anew.
- **A pause that runs past midnight loses the cycle it deferred.** The queue
  only ever holds today's work, so a start deferred before midnight is dropped
  at the resume, and the [watchdog](#missed-cycles-and-late-re-runs) only looks
  back over the current day — so that cycle is neither watered nor recorded as
  missed. Closing the valve the same day avoids it entirely. The safety
  timeout below makes this much harder to reach, but a chain of overlapping
  hand-opens can still cross midnight.

### The safety timeout

A switch you open by hand does not hold the scheduler for ever. Each one gets
a deadline the moment the controller notices it, and when that deadline passes
the controller **switches it off itself** through the same verified path a
cycle uses, raises a `manual_valve_timeout` anomaly (a Repairs issue and one
push notification) and stops holding the scheduler for it. If that was the
last hand-opened switch, anything queued waters right away.

The push is sent when the issue **opens**, like every other
[anomaly](#anomalies). A second timeout on the same switch refreshes the
issue that is already there and sends nothing — so if you keep opening the
same valve and letting it time out, you are told once until you acknowledge
it in Repairs.

- **Manual valve safety timeout** is a controller option: **30 minutes** by
  default, anywhere from **1 to 240**. It cannot be turned off — a valve left
  open all night is exactly what it exists to prevent. Editing it applies
  without restarting Home Assistant. It is the delay before the controller
  *acts*, not a guaranteed ceiling on how long a switch stays open — see the
  limits below.
- **Closing the switch yourself cancels it, silently.** No command, no issue,
  no notification: nothing is said about a pause that ended the way it should.
- **Each switch has its own deadline**, counted from when it was opened. Two
  valves opened twenty minutes apart close twenty minutes apart, and the
  scheduler resumes only after the last one.
- **Re-opening a switch starts a fresh deadline.** Flipping an already-open
  one does not extend it — switch it off and on again for another full session
  on site.
- **The pump counts too.** Opening it by hand pauses the scheduler and gets
  the same deadline as a valve.

Three limits, and none of them is a bug:

- **A switch the running cycle is using is left alone.** If the valve you
  opened is the one the cycle is watering right now, or if it is the pump
  while a cycle runs, the deadline passes and nothing is commanded: closing
  that valve would cut the zone short while the cycle still counts it as
  fully watered, and closing the pump would leave every remaining zone dry.
  The cycle's own close is what shuts it instead, and *when* that comes
  differs by switch: a **valve** is closed at the end of its own slot, so it
  stays open at most for the rest of that zone; the **pump** is only switched
  off when the whole cycle finishes, so it can stay open for the rest of the
  cycle. If that close does not confirm, the switch gets a fresh timeout from
  that moment and the controller tries once more on its own.
- **A reload or a restart drops the deadline along with the pause.** Nothing
  then closes a switch you left open: the controller has forgotten it was open
  by hand. Flipping it again is what starts a new deadline. Since editing any
  controller option reloads the integration, changing the timeout itself is
  one of the things that does this.
- **Editing the duration never moves a deadline already counting down.** A
  deadline is quoted once, when the switch is first noticed. (In practice the
  reload that the edit triggers has already dropped it, per the point above.)

## Anomalies

Everything that goes wrong reaches you through **one pipeline**, and nominal
operation never uses it: completed cycles, rain reductions, skipped or waived
cycles, the season being off, a run-now and reloads produce no notification,
no issue and no event. A nominal week is silent.

When something does go wrong, each anomaly fans out to four places at once:

- a **Repairs issue** (Settings → System → Repairs), one per open anomaly —
  the same fault reported again refreshes that issue rather than adding one;
- **one push notification** to the configured *Notify target*, sent only when
  the anomaly *opens* — never again while it stays open;
- a `ha_irrigation_controller_event` **bus event** with `event_type: anomaly`
  on every report (see the payloads below);
- the controller's **Health** binary sensor (`device_class: problem`), which is
  `on` while at least one anomaly is open.

### Kinds

| Anomaly | Raised when | Clears itself when |
| --- | --- | --- |
| `pump_on_unconfirmed` | the pump switch did not report ON within the actuation timeout | the pump next confirms turning **on** |
| `pump_off_unconfirmed` | the pump switch did not report OFF within the timeout | the pump next confirms turning **off** |
| `valve_open_unconfirmed` | a zone's valve did not report ON within the timeout | that zone's valve next confirms **opening** |
| `valve_close_unconfirmed` | a zone's valve did not report OFF within the timeout | that zone's valve next confirms **closing** |
| `journal_save_failed` | Home Assistant's storage refused a write of the cycle journal | the next journal write succeeds |
| `configured_entity_missing` | a configured pump, valve, sensor or notify target was removed from or disabled in the entity registry | the entity is re-created (enabled) under its stored id or re-enabled; or the role is pointed at another entity or the zone deleted |
| `cycle_interrupted` | a forced reload, a disable or a removal of the integration stopped a running cycle | the cycle is recovered at the next load, or the next cycle completes or is cancelled |
| `cycle_recovered` | a cycle found in progress in the journal at startup was resumed, closed or discarded (see [Restarts and recovery](#restarts-and-recovery)) | never — acknowledge it |
| `missed_cycle` | a scheduled cycle's watering window closed with no record of it — it was re-run at once, or recorded and its watering carried forward as debt (see [Missed cycles and late re-runs](#missed-cycles-and-late-re-runs)) | never — acknowledge it |
| `manual_valve_timeout` | a switch opened by hand stayed open past the [safety timeout](#the-safety-timeout), so the controller switched it off | never — acknowledge it |

Issues are **per subject**: an unconfirmed valve is one issue per zone, an
unconfirmed pump one per pump entity, a missing entity one per zone (for a
valve) or per role (for the pump, the sensors and the notify target), a safety
timeout one per zone (or per entity, for the pump);
`journal_save_failed`, `cycle_interrupted`, `cycle_recovered` and
`missed_cycle` are one issue for the whole controller.

A confirmation clears only the kind it proves: a confirmed ON clears the
`*_on` / `*_open` anomaly of that pump or zone, a confirmed OFF the `*_off` /
`*_close` one. The trivially confirmed close of a valve that never opened
therefore does **not** hide the failed open — that issue stays until the
valve actually confirms opening (usually the next cycle) or you acknowledge
it.

### Acknowledging

An anomaly stays open until it **clears itself** (the table above), or until
you **acknowledge** it in Repairs — either the *Submit* button of its fix
dialog or *Ignore*. Both remove the issue and turn the Health sensor off; if
the same fault happens again later you get a fresh issue and a fresh push.
Acknowledging never changes the schedule: the anomaly is a report, not a
switch.

Issues are not persisted across a Home Assistant restart. They do survive a
reload of the integration, and the pipeline re-reads them on start, so an
options edit does not re-notify you about an anomaly you already know of.
Removing the integration deletes its issues.

### Notify target

The **Notify target** option on the controller is a `notify.*` *entity* — for
the companion app that is `notify.mobile_app_<your phone>`. It is optional and
can be cleared by emptying the picker: without one, anomalies still appear in
Repairs, on the Health sensor and on the bus; they are just not pushed, and
the log says so once at setup. Changing it applies through the normal
cycle-aware reload — no Home Assistant restart. The push is best-effort: a
target that is unavailable or fails is a warning in the log, never a further
anomaly. Legacy `notify.<service>` service names are not supported; pick the
entity.

### Health sensor

The **Health** binary sensor on the controller device is `on` while at least
one anomaly is open and carries two attributes — both read from the `health`
section of the [state view](#state-view-and-websocket-api), the same document
the timeline card receives:

- `open_anomalies` — a list of `{"anomaly": <kind>, ...}` entries with the
  subject keys of each open anomaly (`zone_id`, `entity_id` or `role`);
- `last_anomaly` — the most recent report, kind plus its full context; kept
  after it clears, so you can see what went wrong last.

### Bus events for automations

One event type, discriminated by `event_type`:

```yaml
# Every report — the payload is the anomaly's context plus these two keys
event_type: anomaly
anomaly: valve_open_unconfirmed
cycle_id: 2026-07-31-morning
zone_id: 01J...            # the zone's subentry id
entity_id: switch.zone_1_valve

# Every automatic clear — the kind plus the subject keys only
event_type: anomaly_cleared
anomaly: valve_open_unconfirmed
zone_id: 01J...
```

`anomaly` events fire on every occurrence (including a fault that is already
open); `anomaly_cleared` fires when the engine confirms the subject healthy
again, not when you acknowledge an issue by hand.

## State view and WebSocket API

Everything the integration knows about the controller — the plan, the running
and last cycles, the per-zone water debt, the last seven days of outcomes and
the health state — is composed into **one versioned document**, the *state
view*, built by the engine from the objects it already holds. The four
entities project a few coarse scalars from it; dashboards read the whole
thing over two custom WebSocket commands. There is no second source: what the
card shows and what the entities show come from the same build. The bundled
[dashboard card](#dashboard-card) is this channel's first consumer: it
subscribes once and draws each pushed document.

### Commands

| Command | What it does |
|---|---|
| `ha_irrigation_controller/state_get` | Returns one document. |
| `ha_irrigation_controller/state_subscribe` | Returns the handshake as the result, then the full document as the first `event`, then one `event` per engine push. |

Both take an optional `entry_id`; with a single controller (the only supported
configuration) it can be omitted. An unknown id answers `not_found`; an entry
that exists but has no engine to read right now (unloaded, failed setup, the
torn-down half of a reload) answers `not_loaded` — the same test the
subscription's pushes apply, so a fetch made in answer to a pushed document is
never refused.
**Neither command requires an administrator**: any authenticated user — a
non-admin household member's dashboard included — can fetch and subscribe.
There is no per-user filtering; everybody sees the same document. Commands
(`run_now`, `cancel_cycle`, `set_season`, `set_zone_duration`) stay
[services](#services); the WebSocket channel is read-only.

From a browser console on a dashboard:

```js
await hass.callWS({type: "ha_irrigation_controller/state_get"});
await hass.connection.subscribeMessage(
  console.log,
  {type: "ha_irrigation_controller/state_subscribe"},
);
```

The subscription follows the engine-state signal the entities already use: a
document arrives after every engine step (a cycle start, a zone boundary, a
completion, a cancel, a season change, a manual-override change, an anomaly
raised, cleared or acknowledged, a config edit deferred behind a running
cycle). One engine step can push more than once. While the entry reloads the
subscription stays open: pushes that land while the engine is torn down are
skipped, and the rebuilt engine's first push delivers a fresh document on the
same subscription.

### Handshake

The subscribe result is `{"schema_version": 1, "version": "<manifest version>"}`,
and the same two keys sit at the top of every document, next to
`generated_at`, the UTC instant it was composed. `schema_version` is
the document's schema (bumped when a key changes meaning or disappears —
additions do not bump it); `version` is the integration release. A card that
was built against another version can tell from these that its bundle is
stale.

### The document

Wire conventions: keys are `snake_case`; every instant is a UTC ISO-8601
string; every duration is in seconds; absent is `null`; enums are their
string values; zones are keyed by their subentry id; history rows are keyed
by irrigation day (the Home Assistant-local date of the cycle's configured
start). The document is JSON primitives only.

- `controller` — `entry_id`, `season_enabled`, `reconciled` (startup
  reconciliation done), `manual_override`, `config_change_pending`,
  `deferred` (cycle kinds queued behind a running cycle) and `next_wakeup`
  (the engine's next timer, or `null` before reconciliation).
- `plan` — the configured plan verbatim (`pump_entity_id`, `morning_enabled`,
  `morning_start`, `evening_start` as `HH:MM:SS`, `manual_timeout_s`, the
  `zones` with both durations and the rain inputs) plus `today`: the
  `irrigation_day` and the `cycles` derived on it from the **base**
  durations, each with its `start`, `end` and its zones' planned windows.
  Once a cycle exists, `runs.current` is authoritative for
  it — quoted durations, actual instants — and `plan.today` is what the card
  draws before that.
- `runs` — `current` and `last`, each `null` or a run: `cycle_id`, `kind`,
  `status`, `manual`, `late_rerun`, `recovery`, `configured_start` (the
  plan's intent, what the cycle id and the irrigation day key off) and
  `scheduled_start` (when it was actually dispatched — later for a deferred
  cycle), `pump_entity_id`, `rain_total_mm`, `live_zone_id` (the zone whose slot is
  open, or `null`) and its `zones` — per zone the quoted `duration_s`, the
  `base_s`, `carried_s` and `rain_credit_s` that produced it, the planned
  window, the actual instants and `effective_s`, what it really watered. A
  cancelled cycle is visible here as `runs.last` with `status: "cancelled"`
  and `effective_s: 0` on the zones it never reached.
- `ledger` — `settled_cycle_id`, `day_credit`, `rain_source` and per plan
  zone `deficit_s` and `rain_baseline_mm`.
- `history` — normally fourteen rows at most (seven days, two kinds), oldest
  day first, **one per irrigation day and cycle kind**, each carrying an
  engine-stamped `outcome` (below),
  the `cycle_id` and `status` of the record that supplied it, the markers
  (`manual`, `waived_by`, `recovery`, `late_rerun`), `runs` (how many records
  went into the row), `ended_at`, `rain_total_mm` and the totals `planned_s`,
  `carried_s`, `effective_s`, `rain_credit_s`. The stored history is pruned
  to the retention window on every read, so a row disappears the day it ages
  out even when no cycle has completed since; rows dated in the future (a
  clock corrected backwards) are kept rather than erased.
- `health` — `open`, the open anomalies as `{"anomaly": <kind>, ...}` with
  their subject keys, and `last`, the most recent report with its full
  context, kept after it clears. The same two values the Health sensor shows.

### Outcomes

Six values, stamped once by the engine — the card never re-derives them from
raw records:

| Outcome | When |
|---|---|
| `missed` | The watchdog filed the cycle as never performed (`status: "missed"`). |
| `recovered` | The startup reconciler resumed or closed it (`recovery` set), the watchdog re-ran it late (`late_rerun`), or it was filed `interrupted`. |
| `cancelled` | You stopped it with `cancel_cycle`. |
| `waived` | A completed `run_now` had already watered the day (`waived_by` names it). |
| `reduced` | Rain shortened or skipped at least one zone. |
| `ran` | Everything else. |

When several records share one day and kind — a missed marker and its late
re-run, a run-now and the scheduled cycle it waived, two run-nows — the row
takes the **highest-precedence** outcome, `recovered > missed > cancelled >
waived > reduced > ran`, with `cycle_id`, `status` and totals from the record
that supplied it and `runs` counting them all. Precedence rather than
"latest" so that a later run-now can never bury a miss that was never made
up, or a cancel you may want to see.

### Older records

History records written by earlier releases lack some keys; the reader
defaults them rather than rewriting storage: `planned_s`, `carried_s` and
`rain_credit_s` read 0, `rain_total_mm`, `waived_by` and `recovery` read
`null`, `late_rerun` reads `false`, and `manual` is true only when stored as
exactly `true`. A record whose `status` or `kind` is not a value this release
knows is left out of the view and nothing else is affected.

### Entity attributes stay coarse

The entities read the same view but project only a few scalars from it —
`cycle_id`, `current_zone`, the flags, the per-zone debt figures, the health
lists. The document itself, the timeline and the history never enter an
attribute: the recorder silently drops attribute sets above 16 KB, and the
test suite pins every entity's attributes well under 2 KB.

## Development

The repo ships a devcontainer (Python 3.14 + Node 22). Open it in VS Code and
the `scripts/setup` post-create hook installs everything. Then:

```bash
scripts/develop
```

This starts a local Home Assistant instance (http://localhost:8123) with the
integration loaded **and** a Rollup watch that rebuilds the card bundle to
`custom_components/ha_irrigation_controller/frontend/` on change.

> **Note:** reloading the config entry does *not* pick up Python code changes —
> restart `scripts/develop` (full HA restart) after editing integration code.
> Card changes only need a browser hard-refresh.

Other loops:

```bash
scripts/lint                 # Ruff format + lint (autofix), then mypy
python3 -m pytest tests/     # backend + engine test suites
python3 -m pytest tests/engine  # engine only — must pass with HA uninstalled (AD-1)
cd card && npm run typecheck # tsc --noEmit (vitest does NOT type-check)
cd card && npm test          # card tests (vitest, headless Chromium)
cd card && npm run build     # rebuild the committed card bundle
```

> **Running hassfest locally:** exclude `card/node_modules` from the mount.
> `@vitest/browser` ships a Vite build manifest at
> `node_modules/@vitest/browser/dist/client/.vite/manifest.json`, which hassfest
> mistakes for a second integration and then crashes on. CI is unaffected — the
> hassfest job checks out the repo without installing card dependencies.

### Layout

```text
custom_components/ha_irrigation_controller/
  engine/     # hass-free scheduling core (imports NOTHING from homeassistant.*)
  adapters/   # HA <-> engine adapters
  entities/   # entity platforms
  frontend/   # BUILT card bundle (committed; shipped by HACS)
card/         # card TypeScript source (Lit 3 + Rollup + vitest)
tests/        # pytest-homeassistant-custom-component harness + engine tests
```

The built card bundle is committed on purpose: HACS installs
`custom_components/` as-is, so the card must live inside it (single-repo,
single-install design).

## CI

Every pull request (and every push to `main`) runs hassfest, HACS validation,
Ruff (format + lint), mypy, `tsc`, pytest against HA **stable and beta**, the
engine suite in an environment with **no Home Assistant installed** (the AD-1
gate), and the card build + vitest in headless Chromium. A nightly cron re-runs
all four workflows to catch Home Assistant's monthly breakage early;
`filterwarnings = ["error"]` means a `DeprecationWarning` fails the build while
the removal is still months away.

The HA beta leg is `continue-on-error`: it is an early-warning signal, not a
merge gate. Dependency bumps arrive as grouped dependabot PRs — note that
dependabot cannot bump `hassfest@master` or `hacs/action@main`, since a mutable
ref is not a version.

## License

MIT
