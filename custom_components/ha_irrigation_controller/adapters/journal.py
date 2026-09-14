"""Journal adapter — the ONE implementation of `JournalPort`, ONE Store writer.

AD-2: the engine journals its state on every transition through this adapter
into the single versioned `Store`.

Writes are the whole snapshot; what is read back is the SEED
(`async_load_seed`): the `history` section, the `season_enabled` mode flag and
the water-debt `ledger` section, in ONE read. Machine state — `run`,
`last_run`, `zone_index`, `deferred` — is deliberately never loaded here:
restoring it to resume an in-flight cycle is AD-11's recovery path and it
belongs to Story 3.2, which extends this seam rather than replacing it.

One reader method rather than one per key, deliberately: `Store.async_load`
clears its `_load_future` in a `finally`, so two calls are two reads of the
same document. Story 3.2 grows `JournalSeed` instead of adding a fourth
method.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Final

from homeassistant.helpers.storage import Store

from ..const import DOMAIN, LOGGER  # noqa: TID252
from ..engine.runs import MANUAL_KEY, is_manual  # noqa: TID252
from ..engine.sequencer import JOURNAL_SCHEMA_VERSION  # noqa: TID252

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

# NFR2 wants a SHORT debounce — coalesce the write bursts of a zone boundary
# (close + open + index bump), never defer meaningfully. Seconds.
JOURNAL_SAVE_DEBOUNCE_S: Final = 1

# No entry id in the key: manifest.json declares `single_config_entry: true`,
# so one entry is the only possible shape — and a STABLE key is what makes
# Story 3.2's reconciliation findable after a restart.
STORAGE_KEY: Final = f"{DOMAIN}.journal"


@dataclass(frozen=True, slots=True)
class JournalSeed:
    """What a freshly built `Sequencer` is seeded with after a load.

    Explicitly NOT machine state: outcome records are history, the season is
    a runtime MODE, and the ledger is accounting BETWEEN cycles (Story 2.2).
    None of them resumes an in-flight cycle, so reading them back does not
    make this the AD-11 recovery path — Story 3.2 owns that and extends this
    dataclass.

    `ledger` is the section in the shape `Ledger.as_dict` writes —
    `{"settled_cycle_id": str | None, "deficits": {zone_id: int},
    "day_credit": {"irrigation_day": "YYYY-MM-DD", "cycle_id": str} | None,
    "rain_baselines": {zone_id: float}}` — already validated, so the engine
    can trust it. `day_credit` (Story 2.3) is the irrigation day a completed
    run-now has already watered, with that run-now's id; it is optional on
    read, and a malformed one reads as no credit without touching the rest
    of the section. `rain_baselines` (Story 2.4) is the gauge total, in mm,
    at each zone's previous settled cycle — what the next rain credit is
    measured from; optional on read too, and each malformed entry reads as
    "no baseline" (no credit until the zone settles a cycle) on its own.
    """

    history: list[dict[str, object]]
    season_enabled: bool
    ledger: dict[str, object]


class JournalAdapter:
    """Debounce engine snapshots into the single versioned Store.

    The Store version is the engine's own `JOURNAL_SCHEMA_VERSION` so the two
    can never drift; the payload keeps its own `schema_version` field exactly
    as the engine writes it.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Create the adapter and its Store."""
        self._store: Store[dict[str, object]] = Store(
            hass,
            JOURNAL_SCHEMA_VERSION,
            STORAGE_KEY,
        )
        self._pending: dict[str, object] | None = None
        self._closed = False

    async def async_load_seed(self) -> JournalSeed:
        """Read the seed back out of the ONE document, in ONE read, validated.

        Everything here crosses a trust boundary: `.storage` is unvalidated
        input (a restored backup, a hand edit, schema drift), and a malformed
        `irrigation_day` would raise out of `prune_history` → `_complete_cycle`
        → `advance()`, abandoning a cycle mid-flight with the pump on. So the
        records are filtered to the ones the engine can safely consume rather
        than trusted — the same rule `build_plan` applies to options.

        `season_enabled` gets the same discipline and a fail-wet default: it is
        honoured only when it is a real `bool`, so an absent key (every
        document written before Story 1.6) and the drift that merely looks
        falsy (`None`, `"false"`, `0`) all read as True. Doubt waters (AD-4).

        The document TYPE is part of that boundary: `Store` is only generic in
        its annotation, so a `data` section that is a list or a string is a
        real shape a hand edit or a restored backup can produce. Trusting the
        annotation would raise `AttributeError` out of `async_setup_entry` —
        the opposite of the fail-wet default this method promises.

        Surviving records are NORMALIZED rather than merely filtered, which is
        how a journal written before Story 2.1 keeps loading: it carries no
        manual marker at all (`JOURNAL_SCHEMA_VERSION` has no migration, so
        every key this story adds has to be optional on read), and every such
        record reads back as scheduled instead of failing. Readers therefore
        never have to ask whether the key is there.

        The `ledger` section (Story 2.2) is optional on read for the same
        reason — a document written before it has none — and validated by
        `_seed_ledger`: a malformed section reads as an EMPTY ledger, which
        quotes the next cycle on base durations. Fail-wet (AD-4): the worst a
        corrupt ledger can do is forget a debt, never invent one or crash the
        setup, and nothing is logged for it.
        """
        stored = await self._store.async_load()
        if not isinstance(stored, dict):
            return JournalSeed(history=[], season_enabled=True, ledger=_empty_ledger())
        history = stored.get("history")
        season = stored.get("season_enabled")
        return JournalSeed(
            history=(
                [
                    usable
                    for entry in history
                    if (usable := _seed_entry(entry)) is not None
                ]
                if isinstance(history, list)
                else []
            ),
            season_enabled=season if isinstance(season, bool) else True,
            ledger=_seed_ledger(stored.get("ledger")),
        )

    async def async_save(self, snapshot: dict[str, object]) -> None:
        """Record the snapshot and schedule the debounced write.

        Never an immediate `store.async_save` here: the engine calls this on
        EVERY transition, and a zone boundary is several transitions
        back-to-back — the debounce coalesces them into one write.
        """
        if self._closed:
            # An `advance` still in flight when the entry unloaded — the same
            # window `CycleRunner._rearm`'s shutdown guard exists for. The
            # flush has already persisted the state nothing can move any more,
            # and a delayed write scheduled now would outlive this entry and
            # race the Store of the one replacing it (AD-2: ONE writer).
            LOGGER.debug("Journal closed; dropping a post-unload snapshot")
            return
        self._pending = snapshot
        self._store.async_delay_save(self._pending_snapshot, JOURNAL_SAVE_DEBOUNCE_S)

    async def async_flush(self) -> None:
        """Write the pending snapshot now and close — the entry-unload path.

        `Store` registers its own final-write listener for HA shutdown; this
        flush covers entry-scoped unloads (the reload-per-config-change
        regime), which that listener does not.

        Closing FIRST, before the await: `Store.async_save` yields on the
        executor write, and a save landing in that window would otherwise
        re-arm a delayed write and leave `_pending` inconsistent with it.
        """
        self._closed = True
        if self._pending is not None:
            await self._store.async_save(self._pending)
            self._pending = None

    def _pending_snapshot(self) -> dict[str, object]:
        """Return the snapshot the delayed write should persist, once."""
        snapshot = self._pending
        if snapshot is None:
            # Never reached: a delayed write only exists because a save
            # scheduled it. Raising rather than asserting because `assert` is
            # compiled out under `python -O`, where the failure mode would be
            # `Store` writing `"data": null` over the journal instead.
            msg = "delayed journal write ran with no pending snapshot"
            raise RuntimeError(msg)
        # Consumed: without this, every unload flush would re-write a document
        # the debounced write already persisted.
        self._pending = None
        return snapshot


def _seed_entry(entry: object) -> dict[str, object] | None:
    """Return `entry` fit for the engine, or None when it cannot be consumed.

    Two halves of one trust boundary. The REFUSALS are the shapes that would
    raise out of `prune_history` → `_complete_cycle` → `advance()` and abandon
    a cycle mid-flight with the pump on. The REWRITE fills in the keys later
    stories added, so a document written before them loads with their default
    rather than making every reader defend itself: Story 2.1's manual marker,
    defaulted to scheduled by `is_manual` (the ONE place that rule lives), and
    Story 2.3's `waived_by`, kept only when it is a `str` (the id of the
    run-now that excused a waived cycle) and `None` otherwise — a record that
    ran, or one written before 2.3, reads the same as one `history_entry`
    writes today.

    A SHALLOW copy, never the stored dict mutated in place: the top level is
    rebuilt so the caller's document keeps the keys it had, while nested values
    (a record's `zones` list) are shared with it. Nothing writes through them —
    the engine journals this list straight back out untouched.
    """
    if not isinstance(entry, dict):
        return None
    day = entry.get("irrigation_day")
    if not isinstance(day, str):
        return None
    try:
        date.fromisoformat(day)
    except ValueError:
        return None
    waived_by = entry.get("waived_by")
    return {
        **entry,
        MANUAL_KEY: is_manual(entry),
        "waived_by": waived_by if isinstance(waived_by, str) else None,
    }


def _empty_ledger() -> dict[str, object]:
    """Return the section of a ledger owing nothing, with no credit and no baseline."""
    return {
        "settled_cycle_id": None,
        "deficits": {},
        "day_credit": None,
        "rain_baselines": {},
    }


def _seed_ledger(section: object) -> dict[str, object]:
    """Return the ledger `section` fit for the engine, or an empty ledger.

    The trust boundary for Story 2.2's state, mirroring `_seed_entry`. The
    engine trusts the seed and does arithmetic on it directly, so every value
    that reaches it must already be the right type:

    - `deficits` must be a mapping; each entry is kept only with a `str` key
      and a non-negative `int` value (a `bool` is an `int` to Python and is
      NOT kept). Entries that fail are dropped one by one — a hand edit that
      breaks one zone's debt must not forget every other zone's.
    - `settled_cycle_id` must be a `str` or `None`.
    - `day_credit` (Story 2.3) is optional — absent in every document written
      before it — and kept only as a mapping whose `irrigation_day` parses
      with `date.fromisoformat` and whose `cycle_id` is a `str`; anything else
      reads as NO credit, on its own, without rejecting the section. A bad
      credit must not forget the zones' debts, and the fail-wet reading of a
      doubtful credit is "not credited": the next cycle waters.
    - `rain_baselines` (Story 2.4) is optional — absent in every document
      written before it — and kept entry by entry (`_seed_rain_baselines`):
      a `str` key with a finite, non-negative `int` or `float` value (not a
      `bool`). A bad entry reads as NO baseline for that zone, which credits
      it nothing until it settles a cycle — the fail-wet direction — and
      never rejects the section or the other zones' baselines.

    Anything else about the section's shape — absent (every document written
    before this story), not a mapping, a `deficits` that is not a mapping, a
    settled id of another type — reads as an EMPTY ledger. Fail-wet: an empty
    ledger quotes the next cycle on base durations, which waters.
    """
    if not isinstance(section, dict):
        return _empty_ledger()
    deficits = section.get("deficits")
    settled = section.get("settled_cycle_id")
    if not isinstance(deficits, dict) or not isinstance(settled, str | None):
        return _empty_ledger()
    return {
        "settled_cycle_id": settled,
        "deficits": {
            zone_id: seconds
            for zone_id, seconds in deficits.items()
            if isinstance(zone_id, str)
            and isinstance(seconds, int)
            and not isinstance(seconds, bool)
            and seconds >= 0
        },
        "day_credit": _seed_day_credit(section.get("day_credit")),
        "rain_baselines": _seed_rain_baselines(section.get("rain_baselines")),
    }


def _seed_day_credit(credit: object) -> dict[str, object] | None:
    """Return the day credit fit for the engine, or None when it cannot be.

    The credit half of `_seed_ledger`'s trust boundary. `Ledger.__init__`
    calls `date.fromisoformat` on the day it is handed, so it is parsed here
    first — a malformed day would otherwise raise out of `async_setup_entry`.
    Rebuilt with the two keys only, so a hand edit cannot smuggle extra
    fields into the engine's section.
    """
    if not isinstance(credit, dict):
        return None
    day = credit.get("irrigation_day")
    cycle_id = credit.get("cycle_id")
    if not isinstance(day, str) or not isinstance(cycle_id, str):
        return None
    try:
        date.fromisoformat(day)
    except ValueError:
        return None
    return {"irrigation_day": day, "cycle_id": cycle_id}


def _seed_rain_baselines(baselines: object) -> dict[str, float]:
    """Return the rain baselines fit for the engine — bad entries dropped one by one.

    The baseline half of `_seed_ledger`'s trust boundary (Story 2.4). The
    engine subtracts each value from the gauge total and multiplies by the
    zone's factor, so every value that reaches it must be a real finite
    number: a `str`, a `bool` (an `int` to Python), `nan`, `inf` and a
    negative total are all dropped, each on its own — a hand edit that breaks
    one zone's baseline must not reset every other zone's accumulation
    window. Values are normalized to `float` so the section reads back in the
    one shape `Ledger.as_dict` writes. Anything that is not a mapping at all
    (absent — every document written before this story — or another type)
    reads as no baselines: no credit until each zone settles a cycle, which
    waters.
    """
    if not isinstance(baselines, dict):
        return {}
    return {
        zone_id: float(total)
        for zone_id, total in baselines.items()
        if isinstance(zone_id, str)
        and isinstance(total, int | float)
        and not isinstance(total, bool)
        and math.isfinite(total)
        and total >= 0
    }
