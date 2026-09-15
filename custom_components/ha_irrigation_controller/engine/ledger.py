"""Water-debt ledger — THE one accounting module (AD-5, FR13).

Every cycle is *quoted* through here when it is created and *settled* through
here exactly once when it reaches a terminal status. Nothing else computes a
per-zone effective duration and nothing else writes ledger state: Story 2.3's
day credit, Story 2.4's rain credit and Epic 3's recovery deficits all route
through this module rather than adding parallel arithmetic.

Four operations, deliberately asymmetric:

- ``quote()`` is PURE. It reads the ledger and mutates nothing, so a quoted
  cycle that never settles (crash, suspend, waiver) leaves its deficit AND
  its rain baselines in place: the deficit is re-applied to the next quote
  and the rain stays banked for it — doubt waters (AD-4). It is also the ONE
  place that turns millimetres into seconds (Story 2.4): the rain credit is
  ``floor(max(0, total - baseline) * rain_factor * 60)`` for an exposed zone
  with a baseline, given the gauge total the sequencer read at quote time —
  and only when that total comes from the SAME gauge the baselines were
  banked under (Story 2.5's `rain_source`); a total from another source is
  not comparable to them and credits nothing.
- ``settle(run)`` is the ONLY writer of debt, of the day credit AND of the
  per-zone rain baselines (the gauge total at the zone's previous settled
  cycle) with the source they came from, keyed by the run's cycle id so a
  replay of the same completion is a no-op.
- ``waive(day)`` is the ONLY CONSUMER of the day credit (Story 2.3, FR12):
  one credit, one decision — it clears the credit it consumes, which makes
  it the second writer of ledger state. The `day_credit` property and
  `as_dict` read it too, for the sensor and the journal, and reading
  consumes nothing. The sequencer never inspects the credit itself.
- ``forget_rain()`` (Story 2.5) clears the baselines and their source — the
  third writer, called by the sequencer for an actual season OFF→ON only:
  the operator's statement that a new accumulation period begins. Deficits
  and the day credit are untouched by it.

Three writers of ledger state, then — `settle`, `waive` and `forget_rain` —
and nothing else mutates it.

Hass-free like the rest of the engine: stdlib plus the engine's own types.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from .plan import irrigation_day
from .runs import CycleStatus, effective_seconds

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .plan import CycleKind, ZoneSpec
    from .runs import CycleRun


@dataclass(frozen=True, slots=True)
class ZoneQuote:
    """One zone's quoted duration for a cycle, with the arithmetic shown.

    `quoted_s` is the duration the slot will RUN: ``max(0, base_s -
    rain_credit_s) + carried_s``. Deliberately not called "effective": that
    word belongs to `effective_seconds()` and the history key `effective_s`,
    which mean what a zone ACTUALLY watered. The other fields are the inputs,
    kept so the run snapshot and the history record can say WHY a zone runs as
    long as it does (Epic 4 reads them). `rain_credit_s` is the UNCLAMPED
    worth of the rain (it may exceed `base_s`); the clamp is in `quoted_s`.
    """

    zone_id: str
    base_s: int
    rain_credit_s: int
    carried_s: int
    quoted_s: int


class Ledger:
    """Per-zone water debt with idempotent settlement, day credit, rain baselines.

    State is five things: the outstanding deficit per zone (seconds, strictly
    positive — a zero deficit is simply absent), the id of the last settled
    cycle, at most ONE day credit (Story 2.3) — the irrigation day a
    completed run-now has already watered, with the id of that run-now — the
    rain BASELINE per zone (Story 2.4): the gauge's cumulative total, in mm,
    as it was quoted for the zone's previous settled cycle — and the rain
    SOURCE (Story 2.5): the identity of the gauge every baseline was banked
    from, one for the whole mapping since one quote reads one gauge. The
    sequencer settles only the run it just completed, once, inside
    `_complete_cycle`, so a replay can only ever be of that same run — one id
    is the whole replay defence, and it cannot grow without a pruning rule the
    way a set would. One credit rather than a set for the same reason: the
    next scheduled decision consumes it whatever its day, so nothing ages.
    Baselines are keyed like deficits (the zone id) and pruned like them: the
    settled run's zones REPLACE the mapping, so a removed zone's entry goes.
    """

    def __init__(
        self,
        deficits: Mapping[str, int] | None = None,
        settled_cycle_id: str | None = None,
        day_credit: Mapping[str, object] | None = None,
        rain_baselines: Mapping[str, float] | None = None,
        rain_source: str | None = None,
    ) -> None:
        """Start from `deficits` (zeros dropped), the settled id, credit and baselines.

        `day_credit` is the section's `{"irrigation_day": "YYYY-MM-DD",
        "cycle_id": str}` (or None), already validated by the journal adapter
        at the trust boundary; the `isinstance` checks are type narrowing for
        the serialized `object` values, not validation. `rain_baselines` is
        `{zone_id: mm}` the same way — the adapter has already dropped every
        entry that is not a finite, non-negative number — and `rain_source`
        (Story 2.5) is the gauge identity they were banked under, or None
        for a section written before that story (or one whose source was
        malformed): baselines with no source credit nothing until they are
        re-banked, which waters.
        """
        self._deficits: dict[str, int] = {
            zone_id: seconds
            for zone_id, seconds in (deficits or {}).items()
            if seconds > 0
        }
        self._settled_cycle_id = settled_cycle_id
        # mm at the zone's previous settled cycle; absent = no baseline yet,
        # which quotes with NO credit (the first cycle ever banks, never spends).
        self._rain_baselines: dict[str, float] = {
            zone_id: float(total) for zone_id, total in (rain_baselines or {}).items()
        }
        # The gauge every baseline above came from; None = unknown, so no
        # total is comparable to them and the next settlement re-banks.
        self._rain_source = rain_source
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
        baselines = data.get("rain_baselines")
        source = data.get("rain_source")
        return cls(
            deficits=deficits if isinstance(deficits, dict) else None,
            settled_cycle_id=settled if isinstance(settled, str) else None,
            day_credit=credit if isinstance(credit, dict) else None,
            rain_baselines=baselines if isinstance(baselines, dict) else None,
            rain_source=source if isinstance(source, str) else None,
        )

    def as_dict(self) -> dict[str, object]:
        """Return the serializable ledger section of the journal snapshot.

        `day_credit` is `None` or `{"irrigation_day": "YYYY-MM-DD",
        "cycle_id": str}`, `rain_baselines` is `{zone_id: float}` and
        `rain_source` is `str` or `None` — the shapes `_seed_ledger`
        validates on the way back in (`JOURNAL_SCHEMA_VERSION` stays 1: all
        three keys are optional on read, so a pre-2.3 section loads with no
        credit, a pre-2.4 one with no baselines and a pre-2.5 one with
        baselines but no source — which quotes one unmodulated cycle and
        then modulates).
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
            "rain_baselines": dict(self._rain_baselines),
            "rain_source": self._rain_source,
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

    def rain_baseline_mm(self, zone_id: str) -> float | None:
        """Return the gauge total at `zone_id`'s previous settled cycle, or None.

        READ-ONLY (AD-6). None means the zone has never settled a cycle with
        a readable gauge, so the next quote credits nothing and settlement
        will bank the first baseline.
        """
        return self._rain_baselines.get(zone_id)

    @property
    def rain_source(self) -> str | None:
        """Return the identity of the gauge the baselines were banked from, or None.

        READ-ONLY (AD-6). None means no reading has been banked yet (or the
        section predates Story 2.5), so no total is comparable to the
        baselines and the next quote credits nothing.
        """
        return self._rain_source

    def forget_rain(self) -> None:
        """Clear every rain baseline and the stored source (Story 2.5).

        The third of the three writers of ledger state (`settle`, `waive` —
        which clears the credit it consumes — and this), called by the
        sequencer for an actual season OFF→ON only: the switch is the
        operator's own statement that a new accumulation period begins, so
        the rain banked before it (a winter's worth) must not skip the first
        cycle of the season. The next cycle quotes no credit, waters in full
        and re-banks at settlement — fail-wet. Deficits, the settled id and
        the day credit are untouched: they are about water owed, not rain.
        """
        self._rain_baselines = {}
        self._rain_source = None

    def quote(
        self,
        plan_zones: Sequence[ZoneSpec],
        kind: CycleKind,
        rain_total_mm: float | None = None,
        rain_source: str | None = None,
    ) -> tuple[ZoneQuote, ...]:
        """Return the quoted duration of every zone for a `kind` cycle.

        Pure: reads the ledger, writes nothing. Per zone::

            rain_credit_s = floor(round(max(0, total - baseline) * rain_factor
                                        * 60, 6))
                            iff exposed and factor > 0 and total and baseline
                            are both known and source == the banked source
                            and the product is finite, else 0
            carried_s     = min(deficit, base_s)
            quoted_s      = max(0, base_s - rain_credit_s) + carried_s

        The cap is applied at quote time as well as at settlement: a deficit
        settled on the other kind's (larger) base is capped by THIS kind's
        base, so a zone never waters more than 2x its base whatever it owes.
        The remainder is discarded, not carried — FR13's "capped at one full
        base duration" is a ceiling on the debt, and deficits never compound.

        `rain_total_mm` (Story 2.4) is the gauge's cumulative total as the
        sequencer read it for THIS quote — the same value it snapshots on the
        run — or None when there is no gauge or its reading is doubtful, in
        which case no zone is modulated. THE one place millimetres become
        seconds (AD-5). The rain since the zone's previous settled cycle
        (`total - baseline`) is what is credited; a sheltered zone, a factor
        of 0 and a zone with no baseline yet credit nothing; a total BELOW
        the baseline (a gauge reset) credits nothing rather than a negative
        amount — the clamp is the whole reset policy, there is no "reset
        means rain since zero" inference; and `floor` rounds toward LESS
        credit, which waters more (AD-4). The product is rounded to 6
        decimals BEFORE the floor so a binary-float artefact (`(10.0 - 9.9)
        * 60 = 5.999…`) does not shave a whole second off an ordinary delta;
        a product that is not finite (an absurd total overflowing to `inf`)
        is a doubtful reading and credits 0 rather than raising. The credit
        comes off the base BEFORE the deficit is added and never drives the
        base part below zero: a zone in debt still waters its `carried_s`
        however much it rained — the debt floor holds (FR13).

        `rain_source` (Story 2.5) is the identity of the gauge that produced
        `rain_total_mm`, as the sequencer snapshots it on the run. A total is
        comparable to the baselines only if the SAME gauge produced both, so
        the credit also requires `rain_source` to equal the ledger's stored
        source, both non-None — strict equality, no wildcard. A foreign
        source (the sensor was swapped or renamed), a pre-2.5 section with
        baselines but no source and a ledger with nothing banked all credit
        nothing; settlement then re-banks under the new source and the cycle
        after modulates. This is what stops a replacement gauge with a
        higher lifetime total from crediting that total and skipping every
        exposed zone — the fail-dry outcome FR15 forbids.
        """
        quotes: list[ZoneQuote] = []
        for zone in plan_zones:
            base_s = zone.duration_s(kind)
            credit_s = self._rain_credit_s(zone, rain_total_mm, rain_source)
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

    def _rain_credit_s(
        self,
        zone: ZoneSpec,
        rain_total_mm: float | None,
        rain_source: str | None,
    ) -> int:
        """Return the seconds `rain_total_mm` is worth to `zone` — see `quote`."""
        baseline = self._rain_baselines.get(zone.zone_id)
        if (
            not zone.rain_exposed
            or zone.rain_factor <= 0
            or rain_total_mm is None
            or baseline is None
            or rain_source is None
            or rain_source != self._rain_source
        ):
            return 0
        credit = max(0.0, rain_total_mm - baseline) * zone.rain_factor * 60
        if not math.isfinite(credit):
            return 0
        return math.floor(round(credit, 6))

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

        The RAIN BASELINES (Story 2.4) advance here too, and only here: every
        settled zone's baseline becomes the run's snapshotted `rain_total_mm`
        — the QUOTE-TIME reading, so rain that fell during the cycle is
        banked for the next one — whatever the zone's status. FAILED or
        cancelled included, on purpose: the credit was already subtracted
        from that zone's quote and its shortfall carries as a deficit, so
        keeping the old baseline would credit the same rain twice — the one
        direction fail-wet forbids. A `None` reading is the exception:
        nothing was credited, so nothing is spent and each zone keeps the
        baseline it had. Like deficits, the mapping is REPLACED by the run's
        zones, so a removed zone's baseline is dropped. A waived cycle never
        reaches here, so its rain stays banked for the next real cycle.

        The RAIN SOURCE (Story 2.5) follows the same rule: a non-None reading
        stamps the stored source with the run's snapshotted `rain_source` —
        the gauge that produced the total the baselines are being set to —
        and a None reading leaves it as it was, next to the baselines it
        describes. A foreign source therefore re-banks in one settlement:
        every zone's baseline becomes the new gauge's total and the source
        becomes the new gauge, so the next quote modulates.
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
        total = run.rain_total_mm
        baselines: dict[str, float] = {}
        for zone in run.zone_runs:
            baseline = (
                total if total is not None else self._rain_baselines.get(zone.zone_id)
            )
            if baseline is not None:
                baselines[zone.zone_id] = baseline
        self._rain_baselines = baselines
        if total is not None:
            self._rain_source = run.rain_source
        return True
