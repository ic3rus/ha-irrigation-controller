"""Missed-cycle detection and the watchdog deadline — pure, no I/O (Story 3.3).

Two functions, both decisions on the virtual clock (AD-1): what the plan
EXPECTED of the current irrigation day compared with what the journal
actually records (`missed_cycles`), and the next instant worth looking again
(`next_deadline`). Neither reads a clock, touches a port or mutates anything
— `now` is handed in aware and HA-local like everywhere else in the engine,
and the sequencer is what files records, reports anomalies and dispatches.

The window of a cycle is `derive_schedule(plan, kind, reference).end` and the
day is `irrigation_day(...)`: THE two helpers (AD-5), never new date or time
arithmetic, and never a stored "now + 24 h" — a deadline computed that way
would drift by an hour across each DST transition.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING

from .plan import CycleKind, derive_schedule, irrigation_day

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import date

    from .plan import ControllerPlan
    from .runs import CycleRun


@dataclass(frozen=True, slots=True)
class Miss:
    """One scheduled cycle whose window closed with nothing to show for it.

    `configured_start` is the plan's derived start for that cycle on the
    current irrigation day — the operator's intent, which is what the record
    and the late re-run are both keyed off (never the instant the miss
    happened to be noticed).

    `rerunnable` says whether the cycle is still worth watering: False once
    a LATER cycle of the same irrigation day is already accounted for, so a
    morning noticed at 20:40 after the evening ran is recorded and its
    shortfall booked rather than watered on top of it. At most ONE miss per
    check is ever rerunnable — the latest-starting one — because every other
    detected miss counts as a later cycle for the ones before it.
    """

    kind: CycleKind
    configured_start: datetime
    rerunnable: bool


def enabled_kinds(plan: ControllerPlan) -> tuple[CycleKind, ...]:
    """Return the cycle kinds `plan` actually schedules, in plan order.

    The evening cycle always runs; the morning one is opt-in (spring is
    evening-only) — the same rule `CycleRunner._arm_daily` applies to the
    daily trackers, so a disabled morning cycle is neither started nor
    expected.
    """
    if plan.morning_enabled:
        return (CycleKind.MORNING, CycleKind.EVENING)
    return (CycleKind.EVENING,)


def _reference(now: datetime, day: date) -> datetime:
    """Return a reference instant on `day` in `now`'s timezone.

    `derive_schedule` reads a reference's DATE and its tzinfo and nothing
    else, so midnight is a placeholder here — deliberately built with
    `datetime.combine` rather than by adding a `timedelta` to `now`, since a
    day is not always 24 hours and midnight itself does not exist in every
    zone on every date. Nothing is ever compared against this instant.
    """
    return datetime.combine(day, time(), tzinfo=now.tzinfo)


def next_deadline(plan: ControllerPlan, now: datetime) -> datetime | None:
    """Return the earliest cycle-window end strictly after `now`, or None.

    THE watchdog's time intent, served by the one re-armed point-in-time
    callback through `Sequencer.next_wakeup` (AD-3): no timer of its own.

    Today's window ends and tomorrow's are both derived — at 21:30 today's
    are all in the past and the next look is tomorrow morning — and the
    winner is the earliest one STRICTLY after `now`. Strictly, because the
    re-arm tail calls this again the moment the deadline has been served: an
    instant equal to `now` would be armed, fire at once and be armed again
    forever.

    None only when the plan schedules nothing at all, which cannot happen
    today (the evening cycle is never disabled) — the signature keeps the
    accessor's "no intent" answer available rather than promising an instant
    the plan may one day not have.
    """
    today = irrigation_day(now)
    ends = [
        derive_schedule(plan, kind, _reference(now, day)).end
        for day in (today, today + timedelta(days=1))
        for kind in enabled_kinds(plan)
    ]
    future = [end for end in ends if end > now]
    return min(future) if future else None


def missed_cycles(  # noqa: PLR0913 — every input is a separate permitted cause; bundling them would hide which
    plan: ControllerPlan,
    *,
    now: datetime,
    history: Sequence[Mapping[str, object]],
    season_enabled: bool,
    active: CycleRun | None,
    deferred_kinds: Sequence[CycleKind],
    day_credit: str | None,
) -> tuple[Miss, ...]:
    """Return the cycles of `now`'s irrigation day that were never performed.

    A cycle is MISSED when its window has closed (`now` is at or after
    `derive_schedule(...).end`) and nothing accounts for it. Everything that
    does account for it is a PERMITTED non-watering cause and is silent —
    no record, no anomaly, no re-run:

    - the season is off, the morning cycle is disabled in the plan, or the
      plan has no zones at all (there is no cycle to miss);
    - a history entry already exists for that irrigation day and kind, in
      ANY status: `completed` (rain-skipped zones live inside a completed
      entry), `waived` (Story 2.3's day credit already accounted for the
      water), `cancelled` (the operator said so), `interrupted` (Story 3.2
      booked its shortfalls) — and `missed` itself, which is why a miss is
      detected at most once ever and re-run at most once ever;
    - a live run or a deferred entry for that day and kind: the cycle is
      late, not lost (AD-4 delays, never skips);
    - an unconsumed day credit for that day: a completed run-now already
      watered it and the scheduled cycle is about to be waived.

    The lookback is the CURRENT irrigation day only (Decision 1): an outage
    spanning several days produces misses for the day Home Assistant came
    back and nothing for the days it slept through — those simply have no
    record in the 7-day view.

    The status of a history entry is deliberately NOT inspected beyond its
    existence (Decision 3): a COMPLETED cycle whose zones all watered zero
    seconds counts as performed. This predicate answers "was this cycle
    handled", not "did enough water flow" — under-watering is the ledger's
    question, and it already books it.
    """
    if not season_enabled or not plan.zones:
        return ()
    today = irrigation_day(now)
    starts = {
        kind: derive_schedule(plan, kind, now).start for kind in enabled_kinds(plan)
    }
    performed = {
        entry.get("kind")
        for entry in history
        if entry.get("irrigation_day") == today.isoformat()
    }
    candidates = [
        kind
        for kind in enabled_kinds(plan)
        if now >= derive_schedule(plan, kind, now).end
        and kind.value not in performed
        and kind not in deferred_kinds
        and not _is_active(active, kind, today)
        and day_credit != today.isoformat()
    ]
    # A cycle is only worth watering late while nothing LATER in the same day
    # has already been accounted for — an existing record of the other kind,
    # or the other kind being missed too, which is about to be re-run in its
    # place. Comparing the derived starts rather than the kinds keeps a plan
    # whose evening start precedes its morning one honest.
    accounted = {
        kind for kind in starts if kind.value in performed or kind in candidates
    }
    return tuple(
        Miss(
            kind=kind,
            configured_start=starts[kind],
            rerunnable=not any(
                starts[other] > starts[kind] for other in accounted if other is not kind
            ),
        )
        for kind in candidates
    )


def _is_active(run: CycleRun | None, kind: CycleKind, day: date) -> bool:
    """Return whether `run` IS the `day`/`kind` cycle, live right now."""
    return (
        run is not None
        and run.kind is kind
        and irrigation_day(run.configured_start) == day
    )
