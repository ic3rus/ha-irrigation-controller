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
in the same payload — no separate card install or download. Serving it to
Lovelace (static path + resource registration) arrives with Story 4.2, so the
card is not yet selectable in the dashboard editor.

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

Issues are **per subject**: an unconfirmed valve is one issue per zone, an
unconfirmed pump one per pump entity, a missing entity one per zone (for a
valve) or per role (for the pump, the sensors and the notify target);
`journal_save_failed`, `cycle_interrupted` and `cycle_recovered` are one
issue for the whole controller.

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
one anomaly is open and carries two attributes:

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
