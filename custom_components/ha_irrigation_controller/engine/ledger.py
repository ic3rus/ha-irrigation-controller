"""Water-debt ledger — THE one accounting module (AD-5, FR13).

Every cycle is *quoted* through here when it is created and *settled* through
here exactly once when it reaches a terminal status. Nothing else computes a
per-zone effective duration and nothing else writes ledger state: Story 2.3's
day credit, Story 2.4's rain credit and Epic 3's recovery deficits all route
through this module rather than adding parallel arithmetic.

Three operations, deliberately asymmetric:

- ``quote()`` is PURE. It reads the ledger and mutates nothing, so a quoted
  cycle that never settles (crash, suspend) leaves its deficit in place and
  the deficit is re-applied to the next quote — doubt waters (AD-4).
- ``settle(run)`` is the ONLY writer of debt AND of the day credit, keyed by
  the run's cycle id so a replay of the same completion is a no-op.
- ``waive(day)`` is the ONLY CONSUMER of the day credit (Story 2.3, FR12):
  one credit, one decision. The `day_credit` property and `as_dict` read it
  too, for the sensor and the journal, and reading consumes nothing. The
  sequencer never inspects the credit itself.

Hass-free like the rest of the engine: stdlib plus the engine's own types.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from .plan import irrigation_day
from .runs import CycleStatus, effective_seconds

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .plan import CycleKind, ZoneSpec
    from .runs import CycleRun

# The rain-credit slot of the quote formula, empty. Story 2.4 fills it; until
# then every quote is base + carried deficit. An immutable mapping rather than
# a `{}` default so the shared default can never be written through.
NO_RAIN_CREDIT: Final[Mapping[str, int]] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class ZoneQuote:
    """One zone's quoted duration for a cycle, with the arithmetic shown.

    `quoted_s` is the duration the slot will RUN: ``max(0, base_s -
    rain_credit_s) + carried_s``. Deliberately not called "effective": that
    word belongs to `effective_seconds()` and the history key `effective_s`,
    which mean what a zone ACTUALLY watered. The other fields are the inputs,
    kept so the run snapshot and the history record can say WHY a zone runs as
    long as it does (Epic 4 reads them).
    """

    zone_id: str
    base_s: int
    rain_credit_s: int
    carried_s: int
    quoted_s: int


class Ledger:
    """Per-zone water debt with idempotent settlement, plus the day credit.

    State is three things: the outstanding deficit per zone (seconds, strictly
    positive — a zero deficit is simply absent), the id of the last settled
    cycle, and at most ONE day credit (Story 2.3) — the irrigation day a
    completed run-now has already watered, with the id of that run-now. The
    sequencer settles only the run it just completed, once, inside
    `_complete_cycle`, so a replay can only ever be of that same run — one id
    is the whole replay defence, and it cannot grow without a pruning rule the
    way a set would. One credit rather than a set for the same reason: the
    next scheduled decision consumes it whatever its day, so nothing ages.
    """

    def __init__(
        self,
        deficits: Mapping[str, int] | None = None,
        settled_cycle_id: str | None = None,
        day_credit: Mapping[str, object] | None = None,
    ) -> None:
        """Start from `deficits` (zeros dropped), the settled id and the credit.

        `day_credit` is the section's `{"irrigation_day": "YYYY-MM-DD",
        "cycle_id": str}` (or None), already validated by the journal adapter
        at the trust boundary; the `isinstance` checks are type narrowing for
        the serialized `object` values, not validation.
        """
        self._deficits: dict[str, int] = {
            zone_id: seconds
            for zone_id, seconds in (deficits or {}).items()
            if seconds > 0
        }
        self._settled_cycle_id = settled_cycle_id
        # (credited irrigation day, id of the run-now that credited it).
        self._day_credit: tuple[date, str] | None = None
        if day_credit is not None:
            day = day_credit.get("irrigation_day")
            cycle_id = day_credit.get("cycle_id")
            if isinstance(day, str) and isinstance(cycle_id, str):
                self._day_credit = (date.fromisoformat(day), cycle_id)

    @classmethod
    def from_dict(cls, data: Mapping[str, object] | None) -> Ledger:
        """Rebuild a ledger from its `as_dict` form; `None` is an empty ledger.

        The engine TRUSTS the seed (AD-1: no trust boundary inside the engine):
        the journal adapter has already validated the section's shape and
        values. The `isinstance` checks below are type narrowing for the
        serialized `object` values, not validation — a section the adapter
        would refuse never reaches here.
        """
        if data is None:
            return cls()
        deficits = data.get("deficits")
        settled = data.get("settled_cycle_id")
        credit = data.get("day_credit")
        return cls(
            deficits=deficits if isinstance(deficits, dict) else None,
            settled_cycle_id=settled if isinstance(settled, str) else None,
            day_credit=credit if isinstance(credit, dict) else None,
        )

    def as_dict(self) -> dict[str, object]:
        """Return the serializable ledger section of the journal snapshot.

        `day_credit` is `None` or `{"irrigation_day": "YYYY-MM-DD",
        "cycle_id": str}` — the shape `_seed_ledger` validates on the way
        back in (`JOURNAL_SCHEMA_VERSION` stays 1: the key is optional on
        read, so a pre-2.3 section loads with no credit).
        """
        credit = self._day_credit
        return {
            "settled_cycle_id": self._settled_cycle_id,
            "deficits": dict(self._deficits),
            "day_credit": (
                None
                if credit is None
                else {"irrigation_day": credit[0].isoformat(), "cycle_id": credit[1]}
            ),
        }

    @property
    def settled_cycle_id(self) -> str | None:
        """Return the id of the last settled cycle, read-only (AD-6)."""
        return self._settled_cycle_id

    @property
    def day_credit(self) -> str | None:
        """Return the credited irrigation day as an ISO string, or None.

        READ-ONLY, for the cycle-status sensor's `day_credit` attribute
        (AD-6): the operator can see that a completed run-now has already
        watered the day. Reading it consumes nothing — only `waive` does.
        """
        credit = self._day_credit
        return None if credit is None else credit[0].isoformat()

    def waive(self, day: date) -> str | None:
        """Consume the day credit; return the crediting run-now's id iff it is `day`'s.

        THE one CONSUMER of the credit (Story 2.3, FR12) — `day_credit` and
        `as_dict` read it without touching it — called by the sequencer for
        every scheduled dispatch decision and never for a run-now. One
        credit, one decision: the credit is CLEARED whatever the answer. A
        match means the cycle is waived; a mismatch means the credit was
        stale (recorded on another day and never asked about — the season
        was off, or no later cycle was scheduled that day) and the cycle runs.
        Discarding rather than keeping a stale credit needs no expiry policy,
        and the operator can only ever lose a waiver this way, never water.
        """
        credit = self._day_credit
        self._day_credit = None
        if credit is None:
            return None
        credit_day, cycle_id = credit
        return cycle_id if credit_day == day else None

    def deficit_s(self, zone_id: str) -> int:
        """Return the seconds outstanding for `zone_id` (0 when none)."""
        return self._deficits.get(zone_id, 0)

    def quote(
        self,
        plan_zones: Sequence[ZoneSpec],
        kind: CycleKind,
        rain_credit_s: Mapping[str, int] = NO_RAIN_CREDIT,
    ) -> tuple[ZoneQuote, ...]:
        """Return the quoted duration of every zone for a `kind` cycle.

        Pure: reads the ledger, writes nothing. Per zone::

            carried_s = min(deficit, base_s)
            quoted_s  = max(0, base_s - rain_credit_s) + carried_s

        The cap is applied at quote time as well as at settlement: a deficit
        settled on the other kind's (larger) base is capped by THIS kind's
        base, so a zone never waters more than 2x its base whatever it owes.
        The remainder is discarded, not carried — FR13's "capped at one full
        base duration" is a ceiling on the debt, and deficits never compound.

        `rain_credit_s` is the formula slot for Story 2.4; it is always empty
        in this story.
        """
        quotes: list[ZoneQuote] = []
        for zone in plan_zones:
            base_s = zone.duration_s(kind)
            credit_s = rain_credit_s.get(zone.zone_id, 0)
            carried_s = min(self.deficit_s(zone.zone_id), base_s)
            quotes.append(
                ZoneQuote(
                    zone_id=zone.zone_id,
                    base_s=base_s,
                    rain_credit_s=credit_s,
                    carried_s=carried_s,
                    quoted_s=max(0, base_s - credit_s) + carried_s,
                ),
            )
        return tuple(quotes)

    def settle(self, run: CycleRun) -> bool:
        """Record what a terminal `run` owes; False when already settled.

        THE only write. Per zone::

            new_deficit = clamp(quoted_s - effective_seconds(zone), 0, base_s)

        using the run's snapshotted `base_s` (AD-8: a running cycle completes
        on the parameters it started with), never the live plan. Zero
        deficits are not stored, and the ledger is REPLACED by the settled
        run's zones: a zone no longer in the plan is absent from the run and
        therefore dropped — removed zones do not accumulate.

        `effective_seconds` is the ONE measurement helper. A FAILED open reads
        0 and books a full deficit even if the valve physically opened; a
        late catch-up whose zones opened and closed at the same instant reads
        0 too and carries its full deficit — both are the fail-wet outcome
        (AD-4), and FR13's floor principle tolerates the over-watering.

        The DAY CREDIT (Story 2.3) is recorded here too, and only here: a
        `manual` run that reached COMPLETED and booked no deficit for any zone
        credits the irrigation day of its `configured_start` (the same day
        the cycle id and the history record file it under). "No shortfall"
        rather than "no FAILED zone" on purpose: the settlement outcome is the
        ledger's own definition of under-watering, so a FAILED open, a
        cancel and a zero-dwell catch-up all credit nothing — doubt waters —
        and they leave an existing credit UNTOUCHED (only `waive` consumes).
        A later completed run-now on the same day simply refreshes it.
        """
        if run.cycle_id == self._settled_cycle_id:
            return False
        deficits: dict[str, int] = {}
        for zone in run.zone_runs:
            shortfall_s = zone.duration_s - effective_seconds(zone)
            new_deficit_s = max(0, min(shortfall_s, zone.base_s))
            if new_deficit_s > 0:
                deficits[zone.zone_id] = new_deficit_s
        self._deficits = deficits
        self._settled_cycle_id = run.cycle_id
        if run.manual and run.status is CycleStatus.COMPLETED and not deficits:
            self._day_credit = (irrigation_day(run.configured_start), run.cycle_id)
        return True
