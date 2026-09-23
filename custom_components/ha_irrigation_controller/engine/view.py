"""The ONE versioned, typed state view of the engine (Story 4.1, AD-14).

Everything a dashboard client — or an entity — may want to know about the
controller is composed HERE, on demand, from the public surfaces the engine
already exposes: `Sequencer.plan`, `current_run`, `last_run`,
`deferred_kinds`, `manual_override`, `season_enabled`, `reconciled`,
`ledger`, `next_wakeup(now)` and `history`. The builder reads and never
mutates (AD-6), and it is the only place a projection of engine state is
assembled: the WebSocket fetch, the WebSocket subscription and the four
entities all read this document and nothing else.

Hass-free like the rest of the engine (AD-1): stdlib plus the engine's own
types. The two inputs that live outside the engine — the health projection
(the anomaly manager's open set) and the runner's deferred-reload flag —
enter as plain data arguments.

Wire conventions, held verbatim so the card mirrors the types and adds
nothing: snake_case keys; every instant a UTC ISO-8601 string through
`utc_iso`; every duration in seconds; `null` for absent; enums as their
string values; the zone subentry id as the zone key; history keyed by
irrigation day. The document is JSON primitives only — no `datetime`,
`date`, `Enum` or dataclass leaves — and `json.dumps` round-trips it.

History rides the document rather than a companion fetch: fourteen coarse
rows at most, and one shape for the snapshot and every push. The stored
records carry raw markers (cycle status, `waived_by`, `recovery`,
`late_rerun`, zone `rain_credit_s`); the SIX outcomes the card draws are
stamped ONCE, here, by `outcome_of`, and several records under one
(irrigation day, kind) collapse to one row by `_PRECEDENCE`. The card
never re-derives.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Final, TypedDict, cast

from .history import prune_history
from .plan import CycleKind, derive_schedule, irrigation_day
from .runs import (
    CycleStatus,
    ZoneRunStatus,
    effective_seconds,
    is_manual,
    live_zone,
    utc_iso,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import date, datetime

    from .ledger import Ledger
    from .plan import ControllerPlan, CycleSchedule
    from .runs import CycleRun, ZoneRun
    from .sequencer import Sequencer

# The state-view schema version — the stale-bundle handshake's first half
# (the integration version is the second). Bumped when a key changes
# meaning, is renamed or removed; adding a key is compatible and does not.
STATE_SCHEMA_VERSION: Final = 1


class Outcome(StrEnum):
    """The one outcome a history row carries per (irrigation day, kind).

    FR27's five plus `cancelled` (Decision 1): a cycle the operator stopped
    with `cancel_cycle` is a deliberate act that raised no notification —
    folding it into `missed` would paint it as an anomaly, folding it into
    `ran` would hide it behind a `status` the card is told not to read.
    """

    RAN = "ran"
    REDUCED = "reduced"
    WAIVED = "waived"
    RECOVERED = "recovered"
    MISSED = "missed"
    CANCELLED = "cancelled"


# Collapse precedence when several records share one (irrigation day, kind),
# highest first: `recovered > missed > cancelled > waived > reduced > ran`.
# Precedence rather than "latest record" on purpose — see `history_rows`.
_PRECEDENCE: Final[dict[Outcome, int]] = {
    Outcome.RECOVERED: 5,
    Outcome.MISSED: 4,
    Outcome.CANCELLED: 3,
    Outcome.WAIVED: 2,
    Outcome.REDUCED: 1,
    Outcome.RAN: 0,
}


# --------------------------------------------------------------------------
# The schema — TypedDicts, JSON primitives only
# --------------------------------------------------------------------------


class ControllerView(TypedDict):
    """The controller's mode flags and its next time intent."""

    entry_id: str
    season_enabled: bool
    reconciled: bool
    manual_override: bool
    config_change_pending: bool
    deferred: list[str]
    next_wakeup: str | None


class PlanZoneView(TypedDict):
    """One zone as declared by the operator (`ZoneSpec`)."""

    zone_id: str
    name: str
    valve_entity_id: str
    morning_duration_s: int
    evening_duration_s: int
    rain_exposed: bool
    rain_factor: float


class ZoneWindowView(TypedDict):
    """A zone's derived watering window within one cycle (`ZoneWindow`)."""

    zone_id: str
    start: str
    end: str


class ScheduleView(TypedDict):
    """One cycle's derived schedule on today's plan (`CycleSchedule`)."""

    kind: str
    start: str
    end: str
    zones: list[ZoneWindowView]


class TodayView(TypedDict):
    """The configured plan on `now`'s local day — what the card draws first."""

    irrigation_day: str
    cycles: list[ScheduleView]


class PlanView(TypedDict):
    """The declarative plan plus its derivation for today."""

    pump_entity_id: str
    morning_enabled: bool
    morning_start: str
    evening_start: str
    manual_timeout_s: int
    zones: list[PlanZoneView]
    today: TodayView


class ZoneRunView(TypedDict):
    """One zone's slot in a cycle run (`ZoneRun`), plus what it watered."""

    zone_id: str
    name: str
    valve_entity_id: str
    status: str
    planned_start: str
    planned_end: str
    duration_s: int
    base_s: int
    carried_s: int
    rain_credit_s: int
    actual_start: str | None
    actual_end: str | None
    effective_s: int


class RunView(TypedDict):
    """One cycle run (`CycleRun`): quoted durations, actual instants."""

    cycle_id: str
    kind: str
    status: str
    manual: bool
    late_rerun: bool
    recovery: str | None
    configured_start: str
    scheduled_start: str
    pump_entity_id: str
    rain_total_mm: float | None
    live_zone_id: str | None
    zones: list[ZoneRunView]


class RunsView(TypedDict):
    """The active run and the most recently finished one."""

    current: RunView | None
    last: RunView | None


class LedgerZoneView(TypedDict):
    """One zone's water debt and rain baseline."""

    deficit_s: int
    rain_baseline_mm: float | None


class LedgerView(TypedDict):
    """The per-zone ledger summary — never the journal's `as_dict` shape."""

    settled_cycle_id: str | None
    day_credit: str | None
    rain_source: str | None
    zones: dict[str, LedgerZoneView]


class HistoryRow(TypedDict):
    """One row per (irrigation day, kind), with the engine-stamped outcome."""

    irrigation_day: str
    kind: str
    outcome: str
    cycle_id: str
    status: str
    manual: bool
    waived_by: str | None
    recovery: str | None
    late_rerun: bool
    runs: int
    ended_at: str | None
    rain_total_mm: float | None
    planned_s: int
    carried_s: int
    effective_s: int
    rain_credit_s: int


class HealthView(TypedDict):
    """The open anomalies (kind + subject keys) and the last report."""

    open: list[dict[str, object]]
    last: dict[str, object] | None


class StateView(TypedDict):
    """The whole document: handshake, then the six sections."""

    schema_version: int
    version: str
    generated_at: str
    controller: ControllerView
    plan: PlanView
    runs: RunsView
    ledger: LedgerView
    history: list[HistoryRow]
    health: HealthView


# --------------------------------------------------------------------------
# History — outcomes and rows
# --------------------------------------------------------------------------


def _enum_or_none[E: StrEnum](cls: type[E], value: object) -> E | None:
    """Return `value` as a member of `cls`, or None when it is not one."""
    if not isinstance(value, str):
        return None
    try:
        return cls(value)
    except ValueError:
        return None


def _seconds(value: object) -> int:
    """Read a seconds field of a stored record; anything but an int is 0.

    The legacy default (records written before Stories 2.2 and 2.4 have no
    `planned_s`, `carried_s` or `rain_credit_s`) and the never-raise rule in
    one place. A `bool` is an `int` in Python and is refused like any other
    non-count.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _zones(record: Mapping[str, object]) -> list[Mapping[str, object]]:
    """Return the record's zone records, skipping anything that is not one."""
    zones = record.get("zones")
    if not isinstance(zones, list):
        return []
    return [zone for zone in zones if isinstance(zone, dict)]


def _optional_str(value: object) -> str | None:
    """Read an optional string field: a `str` or None, never anything else."""
    return value if isinstance(value, str) else None


def _optional_number(value: object) -> float | None:
    """Read `rain_total_mm`: a number or None (a `bool` is not a reading)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def outcome_of(record: Mapping[str, object]) -> Outcome | None:  # noqa: PLR0911 — one return per outcome IS the precedence table
    """Stamp the outcome of ONE stored history record, or None if unreadable.

    THE rule (Decision 1), in precedence order per record:

    - `missed` — the status. The watchdog's marker, the miss itself.
    - `recovered` — `recovery` set (the startup reconciler resumed or closed
      it), `late_rerun` true (the watchdog's make-up) or status `interrupted`
      (a closed recovery whose marker was lost across a restart).
    - `cancelled` — the status: the operator stopped it.
    - `waived` — the status: a completed run-now had watered the day.
    - `reduced` — any zone credited rain (`rain_credit_s > 0`) or skipped
      outright (status `skipped`).
    - `ran` — everything else.

    None when the status is not a `CycleStatus` value: such a record is
    dropped from the view by `history_rows`, never raised on. Every other
    field is read with the legacy defaults, so a record written before
    Stories 2.2-3.3 stamps without complaint.
    """
    status = _enum_or_none(CycleStatus, record.get("status"))
    if status is None:
        return None
    if status is CycleStatus.MISSED:
        return Outcome.MISSED
    if (
        _optional_str(record.get("recovery")) is not None
        or record.get("late_rerun") is True
        or status is CycleStatus.INTERRUPTED
    ):
        return Outcome.RECOVERED
    if status is CycleStatus.CANCELLED:
        return Outcome.CANCELLED
    if status is CycleStatus.WAIVED:
        return Outcome.WAIVED
    if any(
        _seconds(zone.get("rain_credit_s")) > 0
        or zone.get("status") == ZoneRunStatus.SKIPPED.value
        for zone in _zones(record)
    ):
        return Outcome.REDUCED
    return Outcome.RAN


def _row(
    irrigation_day_iso: str,
    kind: CycleKind,
    members: Sequence[tuple[Outcome, Mapping[str, object]]],
) -> HistoryRow:
    """Collapse the records filed under one (irrigation day, kind) to a row.

    The record with the highest-precedence outcome supplies the row's
    `cycle_id`, `status`, markers and totals; on equal precedence the LATER
    record wins (two run-nows on one day show the second). `runs` counts
    every record that went into the row.
    """
    outcome, record = members[0]
    for candidate_outcome, candidate in members[1:]:
        if _PRECEDENCE[candidate_outcome] >= _PRECEDENCE[outcome]:
            outcome, record = candidate_outcome, candidate
    zones = _zones(record)
    return {
        "irrigation_day": irrigation_day_iso,
        "kind": kind.value,
        "outcome": outcome.value,
        "cycle_id": str(record["cycle_id"]),
        "status": str(record["status"]),
        "manual": is_manual(record),
        "waived_by": _optional_str(record.get("waived_by")),
        "recovery": _optional_str(record.get("recovery")),
        "late_rerun": record.get("late_rerun") is True,
        "runs": len(members),
        "ended_at": _optional_str(record.get("ended_at")),
        "rain_total_mm": _optional_number(record.get("rain_total_mm")),
        "planned_s": sum(_seconds(zone.get("planned_s")) for zone in zones),
        "carried_s": sum(_seconds(zone.get("carried_s")) for zone in zones),
        "effective_s": sum(_seconds(zone.get("effective_s")) for zone in zones),
        "rain_credit_s": sum(_seconds(zone.get("rain_credit_s")) for zone in zones),
    }


def history_rows(
    records: Sequence[dict[str, object]],
    today: date,
) -> list[HistoryRow]:
    """Build the history rows from the stored records, pruned ON READ.

    `prune_history(records, today)` filters what is shown — the stored list
    is left alone, so a record that aged out since the last completion is
    absent here and still in the journal. A record whose `status` or `kind`
    is not a known enum value, or whose `cycle_id` is not a string, is
    dropped silently and the rest of the view is intact.

    One row per (`irrigation_day`, `kind`), oldest day first (within a day,
    in filing order). Several records under one key — a `missed` marker and
    its late re-run, a run-now and the scheduled cycle's `waived` record, a
    second run-now — collapse to the HIGHEST-precedence outcome, not the
    latest record: "latest" would agree in the two real cases but would
    also let a later manual `ran` bury a `missed` that was never made up.
    """
    groups: dict[tuple[str, CycleKind], list[tuple[Outcome, Mapping[str, object]]]] = {}
    for record in prune_history(list(records), today):
        outcome = outcome_of(record)
        kind = _enum_or_none(CycleKind, record.get("kind"))
        cycle_id = record.get("cycle_id")
        if outcome is None or kind is None or not isinstance(cycle_id, str):
            continue
        key = (str(record["irrigation_day"]), kind)
        groups.setdefault(key, []).append((outcome, record))
    rows = [_row(day, kind, members) for (day, kind), members in groups.items()]
    rows.sort(key=lambda row: row["irrigation_day"])
    return rows


# --------------------------------------------------------------------------
# Plan, runs, ledger
# --------------------------------------------------------------------------


def _instant(moment: datetime) -> str:
    """Serialize an instant that is never absent — through `utc_iso`, typed."""
    return cast("str", utc_iso(moment))


def _schedule_view(schedule: CycleSchedule) -> ScheduleView:
    """Project one derived cycle schedule."""
    return {
        "kind": schedule.kind.value,
        "start": _instant(schedule.start),
        "end": _instant(schedule.end),
        "zones": [
            {
                "zone_id": window.zone_id,
                "start": _instant(window.start),
                "end": _instant(window.end),
            }
            for window in schedule.zones
        ],
    }


def _plan_view(plan: ControllerPlan, now: datetime) -> PlanView:
    """Project the plan and derive today's cycles from its BASE durations.

    `today` is what the card draws before a cycle exists; once a run exists,
    `runs.current` (quoted durations, actual instants) is authoritative for
    that cycle. The morning cycle is listed only when it is enabled.
    """
    return {
        "pump_entity_id": plan.pump_entity_id,
        "morning_enabled": plan.morning_enabled,
        "morning_start": plan.morning_start.isoformat(),
        "evening_start": plan.evening_start.isoformat(),
        "manual_timeout_s": plan.manual_timeout_s,
        "zones": [
            {
                "zone_id": zone.zone_id,
                "name": zone.name,
                "valve_entity_id": zone.valve_entity_id,
                "morning_duration_s": zone.morning_duration_s,
                "evening_duration_s": zone.evening_duration_s,
                "rain_exposed": zone.rain_exposed,
                "rain_factor": zone.rain_factor,
            }
            for zone in plan.zones
        ],
        "today": {
            "irrigation_day": irrigation_day(now).isoformat(),
            "cycles": [
                _schedule_view(derive_schedule(plan, kind, now))
                for kind in CycleKind
                if kind is not CycleKind.MORNING or plan.morning_enabled
            ],
        },
    }


def _zone_run_view(zone: ZoneRun) -> ZoneRunView:
    """Project one zone slot; `effective_s` through the ONE helper (AD-5)."""
    return {
        "zone_id": zone.zone_id,
        "name": zone.name,
        "valve_entity_id": zone.valve_entity_id,
        "status": zone.status.value,
        "planned_start": _instant(zone.planned_start),
        "planned_end": _instant(zone.planned_end),
        "duration_s": zone.duration_s,
        "base_s": zone.base_s,
        "carried_s": zone.carried_s,
        "rain_credit_s": zone.rain_credit_s,
        "actual_start": utc_iso(zone.actual_start),
        "actual_end": utc_iso(zone.actual_end),
        "effective_s": effective_seconds(zone),
    }


def _run_view(run: CycleRun | None) -> RunView | None:
    """Project one run, `live_zone_id` through the engine's ONE `live_zone`."""
    if run is None:
        return None
    live = live_zone(run)
    return {
        "cycle_id": run.cycle_id,
        "kind": run.kind.value,
        "status": run.status.value,
        "manual": run.manual,
        "late_rerun": run.late_rerun,
        "recovery": run.recovery,
        "configured_start": _instant(run.configured_start),
        "scheduled_start": _instant(run.scheduled_start),
        "pump_entity_id": run.pump_entity_id,
        "rain_total_mm": run.rain_total_mm,
        "live_zone_id": None if live is None else live.zone_id,
        "zones": [_zone_run_view(zone) for zone in run.zone_runs],
    }


def _ledger_view(ledger: Ledger, plan: ControllerPlan) -> LedgerView:
    """Summarize the ledger per plan zone — reads only, never `as_dict`."""
    return {
        "settled_cycle_id": ledger.settled_cycle_id,
        "day_credit": ledger.day_credit,
        "rain_source": ledger.rain_source,
        "zones": {
            zone.zone_id: {
                "deficit_s": ledger.deficit_s(zone.zone_id),
                "rain_baseline_mm": ledger.rain_baseline_mm(zone.zone_id),
            }
            for zone in plan.zones
        },
    }


# --------------------------------------------------------------------------
# The builder
# --------------------------------------------------------------------------


def build_view(  # noqa: PLR0913 — the engine plus the four facts it cannot know, each named
    sequencer: Sequencer,
    now: datetime,
    *,
    entry_id: str,
    version: str,
    health: HealthView,
    config_change_pending: bool,
) -> StateView:
    """Compose the whole document from the engine's public surfaces.

    Rebuilt on every read rather than cached: the trigger is a payload-less
    dispatcher signal fanned out to N entities and M subscriptions in
    registration order, so any cache would need an invalidation step
    guaranteed to run first — a second mechanism for a build that walks a
    handful of zones and rows with no I/O.

    `now` is the caller's clock read, aware and HA-local (AD-1): it dates
    `generated_at`, `plan.today` and the history prune, and it is what
    `next_wakeup` is asked against. `version` is the integration's manifest
    version, `health` the anomaly manager's projection and
    `config_change_pending` the runner's deferred-reload flag — the three
    things the engine cannot know, handed in as data.
    """
    plan = sequencer.plan
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "version": version,
        "generated_at": _instant(now),
        "controller": {
            "entry_id": entry_id,
            "season_enabled": sequencer.season_enabled,
            "reconciled": sequencer.reconciled,
            "manual_override": sequencer.manual_override,
            "config_change_pending": config_change_pending,
            "deferred": [kind.value for kind in sequencer.deferred_kinds],
            "next_wakeup": utc_iso(sequencer.next_wakeup(now)),
        },
        "plan": _plan_view(plan, now),
        "runs": {
            "current": _run_view(sequencer.current_run),
            "last": _run_view(sequencer.last_run),
        },
        "ledger": _ledger_view(sequencer.ledger, plan),
        "history": history_rows(sequencer.history, irrigation_day(now)),
        "health": health,
    }
