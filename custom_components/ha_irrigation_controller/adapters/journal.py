"""Journal adapter — the ONE implementation of `JournalPort`, ONE Store writer.

AD-2: the engine journals its state on every transition through this adapter
into the single versioned `Store`. Write-only in Story 1.5 — startup
reconciliation (load/restore) is Story 3.2's, and it is the ONLY recovery
path (AD-11); adding an `async_load` here would create a second recovery
owner that 3.2 would have to delete.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from homeassistant.helpers.storage import Store

from ..const import DOMAIN  # noqa: TID252
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


class JournalAdapter:
    """Debounce engine snapshots into the single versioned Store.

    The Store version is the engine's own `JOURNAL_SCHEMA_VERSION` so the two
    can never drift; the payload keeps its own `schema_version` field exactly
    as the engine writes it.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Create the adapter and its Store (nothing is read back — AD-11)."""
        self._store: Store[dict[str, object]] = Store(
            hass,
            JOURNAL_SCHEMA_VERSION,
            STORAGE_KEY,
        )
        self._pending: dict[str, object] | None = None

    async def async_save(self, snapshot: dict[str, object]) -> None:
        """Record the snapshot and schedule the debounced write.

        Never an immediate `store.async_save` here: the engine calls this on
        EVERY transition, and a zone boundary is several transitions
        back-to-back — the debounce coalesces them into one write.
        """
        self._pending = snapshot
        self._store.async_delay_save(self._pending_snapshot, JOURNAL_SAVE_DEBOUNCE_S)

    async def async_flush(self) -> None:
        """Write the pending snapshot now — the entry-unload path.

        `Store` registers its own final-write listener for HA shutdown; this
        flush covers entry-scoped unloads (the reload-per-config-change
        regime), which that listener does not.
        """
        if self._pending is not None:
            await self._store.async_save(self._pending)
            self._pending = None

    def _pending_snapshot(self) -> dict[str, object]:
        """Return the snapshot the delayed write should persist."""
        # The delayed write only exists because a save scheduled it, and the
        # flush cancels it (Store.async_save cleans the delay listener), so
        # pending can never be None here.
        assert self._pending is not None  # noqa: S101 — internal invariant
        return self._pending
