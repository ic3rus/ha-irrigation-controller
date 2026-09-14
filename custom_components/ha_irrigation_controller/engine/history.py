"""Cycle-outcome history with 7-day retention (AC 4, feeds FR20/FR27).

Lives in the ENGINE, not in the journal adapter, for two reasons: it must be
virtual-clock testable in the bare venv (AD-1), and Epic 4's state view reads
it from the engine rather than from storage (AD-14).

Records are coarse by design — this is history, not a replay log. The full
run objects stay in the snapshot; what survives 7 days is the outcome.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING, Final

from .plan import irrigation_day
from .runs import MANUAL_KEY, effective_seconds, utc_iso

if TYPE_CHECKING:
    from datetime import datetime

    from .runs import CycleRun

# Seven calendar days: today plus the six preceding ones (FR20's "7-day
# history at a glance"). Retention is by irrigation DAY, never by instant.
HISTORY_RETENTION_DAYS: Final = 7


def history_entry(
    run: CycleRun,
    ended_at: datetime,
    *,
    waived_by: str | None = None,
) -> dict[str, object]:
    """Build one compact, serializable outcome record for a finished cycle.

    The irrigation day comes from the ONE `irrigation_day` helper applied to
    `configured_start` — the operator's intent, which never moves, so a cycle
    deferred across midnight stays filed under the day it was scheduled for.

    `ended_at` is the caller's completion instant, NOT the last zone's close:
    the pump-off (FR3) happens after that close, so keying the field off the
    zone would report a cycle duration that excludes its final actuation.

    The manual marker rides along verbatim from the run (Story 2.1): this is
    the ONE place it reaches history, and it is what tells a run-now apart
    from the scheduled cycle of the same kind on the same irrigation day —
    Story 2.3's day credit and Epic 4's history view both key off it.

    Each zone record carries the QUOTED duration (`planned_s`), the deficit
    the ledger applied to it (`carried_s`) and the rain credit it subtracted
    (`rain_credit_s`, Story 2.4) next to what it actually watered (Story
    2.2): Epic 4's history view reads them together to show why a zone ran
    longer or shorter than its base and how much of the plan it met.

    A zone quoted at zero because rain covered it is filed `status:
    "skipped"` with `effective_s: 0` and the credit that skipped it (Story
    2.4). That is how history records WHY nothing flowed: Epic 3's watchdog
    reads a skipped zone with `rain_credit_s > 0` as a PERMITTED
    non-watering cause, like a waived cycle. Records written before 2.4 have
    no `rain_credit_s` key and are not backfilled — Epic 4's reader defaults
    it, as it does `planned_s`.

    `waived_by` (Story 2.3) is the id of the completed run-now whose day
    credit excused this cycle, on a record filed with `status: "waived"`; it
    is written on EVERY record — `None` for a cycle that ran — so Epic 4's
    reader gets one shape. A waived cycle never started, so its zone records
    keep their pre-run values: `status: "pending"`, `effective_s: 0`, and
    `planned_s` the duration the ledger quoted (with `carried_s` the deficit
    it would have applied). Epic 3's watchdog reads a waived record as a
    PERMITTED non-watering cause, never as a missed cycle: the water it
    accounts for flowed in the run-now it names.
    """
    zone_runs = run.zone_runs
    return {
        "cycle_id": run.cycle_id,
        "irrigation_day": irrigation_day(run.configured_start).isoformat(),
        "kind": run.kind.value,
        "status": run.status.value,
        MANUAL_KEY: run.manual,
        "waived_by": waived_by,
        "configured_start": utc_iso(run.configured_start),
        "scheduled_start": utc_iso(run.scheduled_start),
        "ended_at": utc_iso(ended_at),
        "zones": [
            {
                "zone_id": zone.zone_id,
                "status": zone.status.value,
                "planned_s": zone.duration_s,
                "carried_s": zone.carried_s,
                "rain_credit_s": zone.rain_credit_s,
                "effective_s": effective_seconds(zone),
            }
            for zone in zone_runs
        ],
    }


def prune_history(
    entries: list[dict[str, object]],
    today: date,
    days: int = HISTORY_RETENTION_DAYS,
) -> list[dict[str, object]]:
    """Return the entries within the retention window, order preserved.

    A pure filter: `date` in, list out, oldest→newest untouched. Entries
    dated in the FUTURE are kept — a clock that jumped backwards (a restored
    backup, an NTP correction) must not silently erase real history.

    Every entry is one this module built (the journal is write-only in Story
    1.5; loading storage back is Story 3.2's, AD-11), so the day field is
    always the ISO string `history_entry` wrote.
    """
    horizon = timedelta(days=days)
    return [
        entry
        for entry in entries
        if today - date.fromisoformat(str(entry["irrigation_day"])) < horizon
    ]
