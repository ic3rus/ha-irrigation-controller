"""Journal adapter — the ONE implementation of `JournalPort`, ONE Store writer.

AD-2: the engine journals its state on every transition through this adapter
into the single versioned `Store`.

Writes are the whole snapshot; what is read back is the SEED
(`async_load_seed`): the `history` section and the `season_enabled` mode flag,
in ONE read. Machine state — `run`, `last_run`, `zone_index`, `deferred` — is
deliberately never loaded here: restoring it to resume an in-flight cycle is
AD-11's recovery path and it belongs to Story 3.2, which extends this seam
rather than replacing it.

One reader method rather than one per key, deliberately: `Store.async_load`
clears its `_load_future` in a `finally`, so two calls are two reads of the
same document. Story 3.2 grows `JournalSeed` instead of adding a fourth
method.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Final

from homeassistant.helpers.storage import Store

from ..const import DOMAIN, LOGGER  # noqa: TID252
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

    Explicitly NOT machine state: outcome records are history, and the season
    is a runtime MODE. Neither resumes an in-flight cycle, so reading them
    back does not make this the AD-11 recovery path — Story 3.2 owns that and
    extends this dataclass.
    """

    history: list[dict[str, object]]
    season_enabled: bool


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
        """
        stored = await self._store.async_load()
        if not isinstance(stored, dict):
            return JournalSeed(history=[], season_enabled=True)
        history = stored.get("history")
        season = stored.get("season_enabled")
        return JournalSeed(
            history=(
                [entry for entry in history if _is_usable_entry(entry)]
                if isinstance(history, list)
                else []
            ),
            season_enabled=season if isinstance(season, bool) else True,
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


def _is_usable_entry(entry: object) -> bool:
    """Return True iff `entry` is a history record the engine can consume."""
    if not isinstance(entry, dict):
        return False
    day = entry.get("irrigation_day")
    if not isinstance(day, str):
        return False
    try:
        date.fromisoformat(day)
    except ValueError:
        return False
    return True
