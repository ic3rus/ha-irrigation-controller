"""The HA composer of the engine state view — where hass objects meet it.

`engine/view.py` builds the document from the engine's public surfaces and
takes the two things the engine cannot know as plain data: the health
projection and the runner's deferred-reload flag. THIS module is the ONE
place those are read off `entry.runtime_data` and handed over (AD-14): the
WebSocket handlers and the four entity projections all call `current_view`
and nothing else builds any part of the document.

Nothing here is cached and nothing is mutated: a view is composed per call,
on the caller's clock read.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.util import dt as dt_util

from .adapters.anomalies import subject_keys
from .engine.view import build_view

if TYPE_CHECKING:
    from datetime import datetime

    from . import HaIrrigationConfigEntry
    from .adapters.anomalies import AnomalyManager
    from .engine.view import HealthView, StateView


def health_view(anomalies: AnomalyManager) -> HealthView:
    """Project the anomaly manager: open anomalies (kind + subject), last report.

    The shape the health binary sensor has carried since Story 3.1, moved
    here so the entity reads the view like every other surface: `open`
    lists `{anomaly, zone_id?, entity_id?, role?}` — the kind and the
    subject keys, never the full contexts — and `last` is the most recent
    report, kind plus context, kept after it clears.
    """
    last = anomalies.last_anomaly
    return {
        "open": [
            {
                "anomaly": record.kind.value,
                **subject_keys(record.kind, record.context),
            }
            for record in anomalies.open_anomalies
        ],
        "last": None
        if last is None
        else {**dict(last.context), "anomaly": last.kind.value},
    }


def current_view(
    entry: HaIrrigationConfigEntry,
    now: datetime | None = None,
) -> StateView:
    """Compose the state view of a LOADED entry, as of `now`.

    `now` defaults to `dt_util.now()` — aware and HA-local, the engine's
    clock contract (AD-1) and the same accessor `HaClock` uses. Callers that
    already hold a clock read pass it so one push composes one instant.
    """
    data = entry.runtime_data
    return build_view(
        data.sequencer,
        dt_util.now() if now is None else now,
        entry_id=entry.entry_id,
        version=data.version,
        health=health_view(data.anomalies),
        config_change_pending=data.runner.reload_pending,
    )
