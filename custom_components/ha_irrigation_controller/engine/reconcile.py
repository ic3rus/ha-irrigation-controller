"""Startup reconciliation — the PURE recovery decision (Story 3.2, AD-11).

After a Home Assistant restart (or the forced reload of Story 1.7) the journal
still holds the cycle that was in flight. `plan_recovery` decides what to do
with it from three inputs only — the run as journaled, the virtual clock and
the ACTUAL switch states the adapter read back — and re-times the run in
place. No ports, no I/O, no wall clock: the sequencer's `async_reconcile`
performs the orphan pass before calling it and applies the decision after,
which is what keeps this decidable on a virtual clock in the bare venv.

Two rules, from the epic:

- **Same irrigation day → resume.** The cycle continues on its journaled
  `duration_s`/`base_s` (never re-quoted, AD-5) from the slot the crash left
  open, re-timed from `now`. **Later day → close.** The run is filed
  `interrupted` through the normal completion path so the ledger books each
  zone's shortfall exactly once.
- **Credit only what is PROVEN** (fail-wet, AD-4). The live zone's valve
  found ON at recovery means it watered continuously since `actual_start`;
  found OFF or unknown means the watered span cannot be established — on a
  same-day resume the zone is re-run in full, on a later-day close it is
  filed FAILED (0 s, full deficit).

"Irrigation day" is `irrigation_day(configured_start)` vs
`irrigation_day(now)` — the ONE helper, never a new definition.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from .plan import irrigation_day
from .runs import CycleStatus, ZoneRunStatus, live_zone

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from .runs import CycleRun, ZoneRun


class RecoveryOutcome(StrEnum):
    """What the reconciler did with the journaled run — the anomaly's `outcome`.

    DISCARDED is decided by the sequencer, not here: an unreadable run never
    becomes a `CycleRun`, so there is nothing to plan for. It is listed so
    the vocabulary of `CYCLE_RECOVERED.outcome` has one home.
    """

    RESUMED = "resumed"
    CLOSED = "closed"
    DISCARDED = "discarded"


@dataclass(frozen=True, slots=True)
class Recovery:
    """The decision: outcome, the re-timed run, where to continue, what was live.

    `slot` is the index the sequencer continues from on a RESUMED outcome —
    `len(run.zone_runs)` when every slot is already spent and the cycle only
    needs completing. On a CLOSED outcome the sequencer completes the run
    whatever the slot says. `live_zone_id` is the zone whose slot the crash
    left open (None when none was) — the `zone_id` of the anomaly.
    """

    outcome: RecoveryOutcome
    run: CycleRun
    slot: int
    live_zone_id: str | None


def plan_recovery(
    run: CycleRun,
    *,
    now: datetime,
    states: Mapping[str, bool | None],
    season_enabled: bool,
) -> Recovery:
    """Decide how to recover `run` at `now` given the actual `states`; re-time it.

    `states` maps each governed entity id to what the switch port read back:
    True on, False off, None doubtful (missing, `unknown`, `unavailable`).
    Only the live zone's valve is consulted here — the orphan pass that
    closes whatever is not OFF is the sequencer's, and it runs BEFORE this.

    The run is MUTATED in place (statuses, instants, windows) and handed back
    inside the `Recovery`; nothing is copied, so the sequencer's `self._run`
    is the object decided about.

    Matrix (spec 3.2), by journaled status:

    - PENDING, same day, season ON → RESUMED from slot 0: `scheduled_start`
      becomes `now` and every window is re-timed from it, so the normal
      `_start_cycle` path starts it at once.
    - PENDING otherwise (a later day, or season OFF — nothing was ever
      commanded, and 1.6's "a cycle in progress completes" covers RUNNING
      only) → CLOSED, every zone untouched: full deficits, capped by the
      ledger.
    - RUNNING, same day (season OFF included — in progress completes):
        - live valve ON, slot not elapsed → the zone stays RUNNING with its
          `actual_start`, `planned_end = actual_start + duration_s` (what it
          would always have been), the sequencer re-opens it; the rest
          re-timed back-to-back from that end.
        - live valve ON, slot elapsed → COMPLETED at `now` (the over-watering
          is booked honestly), resume from the next slot re-timed from `now`.
        - live valve OFF or unknown → the zone is reset to PENDING (no
          `actual_start`, no `open_confirmed`) and re-run in full from `now`;
          the rest re-timed after it.
        - no live zone (crash between zones) → resume at the first PENDING
          slot re-timed from `now`; none left → `slot == len(zone_runs)`.
    - RUNNING, later day → CLOSED: the live zone is stamped (valve ON →
      COMPLETED at `now`; OFF or unknown → FAILED, 0 s), pending zones are
      left untouched (full deficit).
    """
    same_day = irrigation_day(run.configured_start) == irrigation_day(now)
    if run.status is not CycleStatus.PENDING:
        return _plan_running(run, now=now, states=states, same_day=same_day)
    if same_day and season_enabled:
        run.scheduled_start = now
        _retime(run.zone_runs, 0, now)
        return Recovery(RecoveryOutcome.RESUMED, run, 0, None)
    return Recovery(RecoveryOutcome.CLOSED, run, 0, None)


def _plan_running(
    run: CycleRun,
    *,
    now: datetime,
    states: Mapping[str, bool | None],
    same_day: bool,
) -> Recovery:
    """Decide the RUNNING half of `plan_recovery` — see its matrix."""
    zones = run.zone_runs
    live = live_zone(run)
    if live is None:
        if not same_day:
            return Recovery(RecoveryOutcome.CLOSED, run, len(zones), None)
        slot = next(
            (
                index
                for index, zone in enumerate(zones)
                if zone.actual_start is None and zone.status is ZoneRunStatus.PENDING
            ),
            len(zones),
        )
        _retime(zones, slot, now)
        return Recovery(RecoveryOutcome.RESUMED, run, slot, None)

    index = zones.index(live)
    # Proven means the valve reads ON *and* the open was confirmed: a FAILED
    # slot stays FAILED at a boundary (`_finish_zone`), so it is not credited
    # here either — same day it is re-run in full, later day it reads 0 s.
    proven = (
        states.get(live.valve_entity_id) is True
        and live.status is not ZoneRunStatus.FAILED
    )
    if not same_day:
        if proven:
            live.actual_end = now
            live.status = ZoneRunStatus.COMPLETED
        else:
            live.status = ZoneRunStatus.FAILED
        return Recovery(RecoveryOutcome.CLOSED, run, index, live.zone_id)

    # `live_zone` found this zone by its `actual_start`, so the None branch
    # is unreachable — kept in the condition rather than asserted, since an
    # `assert` is compiled out under `python -O` and this is the fail-wet arm.
    started = live.actual_start
    if not proven or started is None:
        live.actual_start = None
        live.open_confirmed = None
        live.status = ZoneRunStatus.PENDING
        _retime(zones, index, now)
        return Recovery(RecoveryOutcome.RESUMED, run, index, live.zone_id)

    end = _add_seconds(started, live.duration_s)
    if now >= end:
        live.actual_end = now
        live.status = ZoneRunStatus.COMPLETED
        _retime(zones, index + 1, now)
        return Recovery(RecoveryOutcome.RESUMED, run, index + 1, live.zone_id)
    live.status = ZoneRunStatus.RUNNING
    live.planned_end = end
    _retime(zones, index + 1, end)
    return Recovery(RecoveryOutcome.RESUMED, run, index, live.zone_id)


def _retime(zones: Sequence[ZoneRun], start: int, at: datetime) -> None:
    """Lay the slots from `start` back-to-back from `at`; finished ones untouched.

    Durations are ELAPSED seconds, so the accumulation runs in UTC and the
    windows are converted back to `at`'s timezone — the same reason
    `plan.zone_windows` does (a `ZoneInfo` wall-clock addition across a DST
    transition would water an hour short or an hour long).
    """
    cursor = at
    for zone in zones[start:]:
        zone.planned_start = cursor
        cursor = _add_seconds(cursor, zone.duration_s)
        zone.planned_end = cursor


def _add_seconds(moment: datetime, seconds: int) -> datetime:
    """Return `moment + seconds` as elapsed time, in `moment`'s timezone."""
    return (moment.astimezone(UTC) + timedelta(seconds=seconds)).astimezone(
        moment.tzinfo
    )
