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
deficit survives a reload and a Home Assistant restart.

`run_now` uses the same arithmetic: a manual cycle applies, and then clears,
outstanding deficits exactly like a scheduled one.

Each zone's **Last watering duration** sensor carries two attributes:
`carried_deficit`, the seconds that were added to the run the sensor is
showing, and `pending_deficit`, the seconds the zone is owed according to the
ledger. The ledger only moves when a cycle completes (or is cancelled), so
while the run that is paying a debt is still in progress `pending_deficit`
keeps showing that debt; it drops once the run finishes.

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
the pump, and a `cycle_interrupted` anomaly is raised. The interrupted cycle
is not resumed in this version — but the hardware is left safe and you are
told.

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
  raises the usual unconfirmed-actuation anomalies. Like every anomaly in this
  version, it stays open until the integration reloads.

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
