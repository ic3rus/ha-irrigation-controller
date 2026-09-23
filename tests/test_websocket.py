"""The WebSocket read channel: `state_get` and `state_subscribe` (Story 4.1).

What is proven here is DELIVERY — the schema itself is proven where it lives,
in `tests/engine/test_view.py`. Every test drives a real websocket client
against a real entry: an admin fetch, a READ-ONLY user's subscription (the
non-admin household member is a first-class case, not an afterthought), one
event per engine push, the two error codes, a reload under a live
subscription, and the dispatcher connection released on disconnect.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from types import NoneType
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import DATA_DISPATCHER, async_dispatcher_send
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.ha_irrigation_controller
from custom_components.ha_irrigation_controller.const import (
    ATTR_CYCLE,
    CONF_EVENING_START,
    DOMAIN,
    SERVICE_CANCEL_CYCLE,
    SERVICE_RUN_NOW,
    WS_TYPE_STATE_GET,
    WS_TYPE_STATE_SUBSCRIBE,
    engine_state_signal,
)
from custom_components.ha_irrigation_controller.engine.ports import AnomalyKind
from custom_components.ha_irrigation_controller.engine.view import (
    STATE_SCHEMA_VERSION,
)
from custom_components.ha_irrigation_controller.state_view import (
    current_view,
    health_view,
)
from tests.common import (
    CONTROLLER_OPTIONS,
    PUMP,
    VALVE_1,
    VALVE_2,
    fire_at,
    register_switch_domain,
    zone_subentry_data,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.typing import (
        MockHAClientWebSocket,
        WebSocketGenerator,
    )

MANIFEST_VERSION = json.loads(
    (
        Path(custom_components.ha_irrigation_controller.__file__).parent
        / "manifest.json"
    ).read_text(encoding="utf-8"),
)["version"]

SECTIONS = {"controller", "plan", "runs", "ledger", "history", "health"}
SNAKE_CASE = re.compile(r"^[a-z][a-z0-9_]*$")


@pytest.fixture(autouse=True)
def quiet_access_log() -> Iterator[None]:
    """Silence aiohttp's access log for the duration of each test here.

    Under a frozen clock `time.localtime()` reports no `tm_gmtoff`, and the
    access logger's local-time formatting trips over it — aiohttp swallows
    the error but logs it at ERROR, which the reload test below would read
    as a failure of ours. The access lines are noise in a test run anyway.
    """
    logger = logging.getLogger("aiohttp.access")
    level = logger.level
    logger.setLevel(logging.WARNING)
    yield
    logger.setLevel(level)


@pytest.fixture(autouse=True)
def frozen_clock(freezer: FrozenDateTimeFactory, paris: None) -> None:
    """Freeze every test here at 06:59 Paris, BEFORE any access token is minted.

    Autouse so it runs ahead of PHCC's token fixtures: an access token is a
    JWT stamped with `iat`/`exp` from the clock it was created on, and a
    clock moved into the past afterwards makes the token look issued in the
    future — `auth_invalid` at connect. Tests move the clock forward from
    here; the socket authenticates once, at connect, so expiry never bites.
    """
    freezer.move_to("2026-07-31 06:59:00+02:00")


@pytest.fixture
async def entry(hass: HomeAssistant) -> MockConfigEntry:
    """Set up a two-zone controller just before its morning start."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options=dict(CONTROLLER_OPTIONS),
        subentries_data=[
            {**zone_subentry_data("Zone A", VALVE_1), "subentry_id": "zone-a"},
            {**zone_subentry_data("Zone B", VALVE_2), "subentry_id": "zone-b"},
        ],
    )
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry


async def subscribe(
    client: MockHAClientWebSocket,
    **extra: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Subscribe and return (the handshake result, the first document)."""
    await client.send_json_auto_id({"type": WS_TYPE_STATE_SUBSCRIBE, **extra})
    result = await client.receive_json()
    assert result["success"], result
    first = await client.receive_json()
    assert first["type"] == "event"
    assert first["id"] == result["id"]
    return result["result"], first["event"]


async def pushed_documents(client: MockHAClientWebSocket) -> list[dict[str, Any]]:
    """Drain every document pushed so far, in order; a fetch marks the end.

    Messages on one connection are ordered, so a `state_get` sent now is
    answered AFTER every event the engine had already pushed: read until its
    result and everything before it is what the last step pushed. One engine
    step may push more than once (the runner pushes after the step AND after
    each governed-switch observation the step itself caused), so callers read
    the LAST document as the step's outcome and never assume a count.
    """
    await client.send_json_auto_id({"type": WS_TYPE_STATE_GET})
    documents: list[dict[str, Any]] = []
    while True:
        message = await client.receive_json()
        if message["type"] == "result":
            assert message["success"], message
            return documents
        assert message["type"] == "event", message
        documents.append(message["event"])


async def last_document(client: MockHAClientWebSocket) -> dict[str, Any]:
    """Return the most recent pushed document; there must be at least one."""
    documents = await pushed_documents(client)
    assert documents, "the step pushed nothing"
    return documents[-1]


def dispatcher_targets(hass: HomeAssistant, entry_id: str) -> int:
    """Count the listeners on the entry's engine-state signal (entities + us)."""
    dispatchers: dict[str, dict[Any, Any]] = hass.data.get(DATA_DISPATCHER, {})
    return len(dispatchers.get(engine_state_signal(entry_id), {}))


def assert_snake_case(node: object, *, path: str = "$") -> None:
    """Every SCHEMA key is snake_case; the zone-id keys of `ledger.zones` are data."""
    if isinstance(node, dict):
        for key, value in node.items():
            if path != "$.ledger.zones":
                assert SNAKE_CASE.match(key), f"{path}.{key}"
            assert_snake_case(value, path=f"{path}.{key}")
    elif isinstance(node, list):
        for item in node:
            assert_snake_case(item, path=f"{path}[]")


async def test_state_get_returns_the_one_document_the_view_builds(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    entry: MockConfigEntry,
) -> None:
    """AC 1: one document, every section from `engine/view.py`, the handshake on top."""
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": WS_TYPE_STATE_GET})
    message = await client.receive_json()

    assert message["success"], message
    document = message["result"]
    assert document["schema_version"] == STATE_SCHEMA_VERSION
    assert document["version"] == MANIFEST_VERSION == entry.runtime_data.version
    assert set(document) >= SECTIONS
    assert document["controller"]["entry_id"] == entry.entry_id
    assert document["runs"] == {"current": None, "last": None}
    assert document["health"] == {"open": [], "last": None}
    # The wire document IS the composed view — nothing is added or reshaped
    # between `build_view` and the socket (the clock is frozen, so
    # `generated_at` agrees too).
    assert document == json.loads(json.dumps(current_view(entry)))

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_read_only_user_gets_the_handshake_then_the_full_document(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_read_only_access_token: str,
    entry: MockConfigEntry,
) -> None:
    """AC 2, matrix "Subscribe, non-admin": never `unauthorized`, snake_case keys."""
    client = await hass_ws_client(hass, access_token=hass_read_only_access_token)

    handshake, document = await subscribe(client)

    assert handshake == {
        "schema_version": STATE_SCHEMA_VERSION,
        "version": MANIFEST_VERSION,
    }
    assert set(document) >= SECTIONS
    assert document["schema_version"] == STATE_SCHEMA_VERSION
    assert document["version"] == MANIFEST_VERSION
    assert [zone["zone_id"] for zone in document["plan"]["zones"]] == [
        "zone-a",
        "zone-b",
    ]
    assert set(document["ledger"]["zones"]) == {"zone-a", "zone-b"}
    assert_snake_case(document)

    await client.send_json_auto_id({"type": WS_TYPE_STATE_GET})
    fetched = await client.receive_json()
    assert fetched["success"], fetched

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_every_engine_push_yields_exactly_one_event(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    entry: MockConfigEntry,
) -> None:
    """AC 2: one push of the entry's signal, one event — no more, no fewer.

    The signal is sent by hand so the count is exact, and a `state_get`
    sentinel right after proves nothing else was queued behind the event.
    """
    client = await hass_ws_client(hass)
    await subscribe(client)

    async_dispatcher_send(hass, engine_state_signal(entry.entry_id))
    assert len(await pushed_documents(client)) == 1

    async_dispatcher_send(hass, engine_state_signal(entry.entry_id))
    async_dispatcher_send(hass, engine_state_signal(entry.entry_id))
    assert len(await pushed_documents(client)) == 2

    # Another entry's signal is not ours.
    async_dispatcher_send(hass, engine_state_signal("someone-else"))
    assert await pushed_documents(client) == []

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_pushed_documents_follow_a_timer_driven_cycle(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    freezer: FrozenDateTimeFactory,
    entry: MockConfigEntry,
) -> None:
    """Matrix "Push mid-cycle": the daily start opens zone A, the boundary zone B."""
    register_switch_domain(hass)
    client = await hass_ws_client(hass)
    _, idle = await subscribe(client)
    assert idle["runs"]["current"] is None

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    running = await last_document(client)
    current = running["runs"]["current"]
    assert current is not None
    assert current["status"] == "running"
    assert current["live_zone_id"] == "zone-a"
    zone_a, zone_b = current["zones"]
    assert zone_a["status"] == "running"
    assert zone_a["actual_start"] == "2026-07-31T05:00:00+00:00"
    assert zone_b["status"] == "pending"
    assert running["controller"]["next_wakeup"] == "2026-07-31T05:10:00+00:00"

    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")
    advanced = await last_document(client)
    current = advanced["runs"]["current"]
    assert current is not None
    assert current["live_zone_id"] == "zone-b"
    zone_a, zone_b = current["zones"]
    assert zone_a["status"] == "completed"
    assert zone_a["effective_s"] == 600
    assert zone_b["status"] == "running"
    assert zone_b["actual_start"] == "2026-07-31T05:10:00+00:00"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_cancel_shows_in_the_next_document_and_in_history(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    freezer: FrozenDateTimeFactory,
    entry: MockConfigEntry,
) -> None:
    """AC 3, matrix "Cancel": `runs.last.status` cancelled and a `cancelled` row."""
    register_switch_domain(hass)
    client = await hass_ws_client(hass)
    await subscribe(client)
    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    await pushed_documents(client)

    freezer.move_to("2026-07-31 07:05:00+02:00")
    await hass.services.async_call(DOMAIN, SERVICE_CANCEL_CYCLE, blocking=True)
    await hass.async_block_till_done()

    document = await last_document(client)
    assert document["runs"]["current"] is None
    last = document["runs"]["last"]
    assert last is not None
    assert last["status"] == "cancelled"
    zone_a, zone_b = last["zones"]
    assert zone_a["effective_s"] == 300
    assert zone_b["effective_s"] == 0
    assert zone_b["actual_start"] is None
    (row,) = document["history"]
    assert row["irrigation_day"] == "2026-07-31"
    assert row["kind"] == "morning"
    assert row["outcome"] == "cancelled"
    assert row["status"] == "cancelled"
    assert row["cycle_id"] == "2026-07-31-morning"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_health_rides_the_document_and_clears_on_acknowledge(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    entry: MockConfigEntry,
) -> None:
    """Matrix "Health": the open anomaly, then the Repairs dismissal, `last` kept."""
    client = await hass_ws_client(hass)
    await subscribe(client)
    context: dict[str, object] = {
        "cycle_id": "2026-07-31-morning",
        "zone_id": "zone-a",
        "entity_id": VALVE_1,
    }

    entry.runtime_data.anomalies.report(AnomalyKind.VALVE_OPEN_UNCONFIRMED, context)
    await hass.async_block_till_done()

    health = (await last_document(client))["health"]
    assert health["open"] == [
        {"anomaly": "valve_open_unconfirmed", "zone_id": "zone-a"}
    ]
    assert health["last"] == {**context, "anomaly": "valve_open_unconfirmed"}

    ir.async_delete_issue(hass, DOMAIN, "valve_open_unconfirmed:zone-a")
    await hass.async_block_till_done()

    health = (await last_document(client))["health"]
    assert health["open"] == []
    assert health["last"] == {**context, "anomaly": "valve_open_unconfirmed"}

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize("command", [WS_TYPE_STATE_GET, WS_TYPE_STATE_SUBSCRIBE])
async def test_an_unknown_entry_id_is_not_found(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    entry: MockConfigEntry,
    command: str,
) -> None:
    """Matrix "Unknown entry": `not_found`, a WS error only, nothing subscribed."""
    client = await hass_ws_client(hass)
    before = dispatcher_targets(hass, entry.entry_id)

    await client.send_json_auto_id({"type": command, "entry_id": "nope"})
    message = await client.receive_json()

    assert message["success"] is False
    assert message["error"]["code"] == "not_found"
    assert dispatcher_targets(hass, entry.entry_id) == before

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_no_entry_at_all_is_not_found(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """The commands outlive the entry (registered in `async_setup`) and say so."""
    assert await async_setup_component(hass, DOMAIN, {})
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": WS_TYPE_STATE_GET})
    message = await client.receive_json()

    assert message["success"] is False
    assert message["error"]["code"] == "not_found"


async def test_an_unloaded_entry_is_not_loaded(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    entry: MockConfigEntry,
) -> None:
    """Matrix "Entry not loaded": `not_loaded`, and no subscription is stored."""
    client = await hass_ws_client(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert dispatcher_targets(hass, entry.entry_id) == 0

    await client.send_json_auto_id({"type": WS_TYPE_STATE_SUBSCRIBE})
    message = await client.receive_json()

    assert message["success"] is False
    assert message["error"]["code"] == "not_loaded"
    assert dispatcher_targets(hass, entry.entry_id) == 0

    await client.send_json_auto_id(
        {"type": WS_TYPE_STATE_GET, "entry_id": entry.entry_id},
    )
    message = await client.receive_json()
    assert message["error"]["code"] == "not_loaded"


async def test_a_reload_under_a_live_subscription_skips_the_gap_and_resumes(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    freezer: FrozenDateTimeFactory,
    entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC 6, matrix "Reload while subscribed".

    An idle options edit reloads the entry. Between the unload and the next
    setup `runtime_data` is gone and any push is skipped, never raised on —
    every document that DID arrive is whole and carries the new plan, so none
    was composed from a torn-down entry. The reload's own reconcile push
    delivers the NEW plan on the SAME subscription, and later engine steps
    keep arriving on it.
    """
    register_switch_domain(hass)
    client = await hass_ws_client(hass)
    _, before = await subscribe(client)
    assert before["plan"]["evening_start"] == "20:00:00"
    runtime_before = entry.runtime_data

    hass.config_entries.async_update_entry(
        entry,
        options={**CONTROLLER_OPTIONS, CONF_EVENING_START: "21:30:00"},
    )
    await hass.async_block_till_done()

    assert entry.runtime_data is not runtime_before  # it really reloaded
    documents = await pushed_documents(client)
    assert documents, "the reload's reconcile push never arrived"
    for document in documents:
        assert document["plan"]["evening_start"] == "21:30:00"
        assert document["controller"]["reconciled"] is True
        assert document["controller"]["config_change_pending"] is False
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]

    # The subscription is the same one: the next engine step still arrives.
    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    running = await last_document(client)
    current = running["runs"]["current"]
    assert current is not None
    assert current["live_zone_id"] == "zone-a"
    assert running["plan"]["evening_start"] == "21:30:00"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def assert_json_leaves(node: object, *, path: str = "$") -> None:
    """Every leaf is a JSON primitive — `type(...) is`, so a StrEnum fails too."""
    if isinstance(node, dict):
        for key, value in node.items():
            assert type(key) is str, f"{path}.{key!r}"
            assert_json_leaves(value, path=f"{path}.{key}")
    elif isinstance(node, list):
        for item in node:
            assert_json_leaves(item, path=f"{path}[]")
    else:
        assert type(node) in (str, int, float, bool, NoneType), (path, node)


@pytest.mark.parametrize("command", [WS_TYPE_STATE_GET, WS_TYPE_STATE_SUBSCRIBE])
async def test_an_explicit_entry_id_addresses_the_loaded_entry(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    entry: MockConfigEntry,
    command: str,
) -> None:
    """The `entry_id` hit: both commands resolve the entry they are told."""
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": command, "entry_id": entry.entry_id})
    message = await client.receive_json()

    assert message["success"], message
    if command == WS_TYPE_STATE_GET:
        document = message["result"]
    else:
        assert message["result"]["schema_version"] == STATE_SCHEMA_VERSION
        document = (await client.receive_json())["event"]
    assert document["controller"]["entry_id"] == entry.entry_id

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_push_landing_while_the_entry_is_torn_down_is_skipped(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The mid-reload gap, forced by hand: unloaded entry, a push, no event, no error.

    The signal is sent while the entry is NOT_LOADED with no `runtime_data`,
    which is exactly the window a reload opens; only the documents the next
    setup's own reconcile pushes may arrive, and every one of them is whole.
    """
    client = await hass_ws_client(hass)
    await subscribe(client)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert not hasattr(entry, "runtime_data")
    async_dispatcher_send(hass, engine_state_signal(entry.entry_id))
    await hass.async_block_till_done()

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    documents = await pushed_documents(client)
    assert documents, "the setup's reconcile push never arrived"
    for document in documents:
        assert document["controller"]["reconciled"] is True
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_push_with_no_runtime_data_on_a_loaded_entry_is_skipped(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The attribute guard on its own: LOADED but no `runtime_data` → skipped.

    An artificial state Home Assistant never produces (it deletes
    `runtime_data` only after unloading), so the ENTITIES on the same signal,
    which have no such guard, do raise here — the dispatcher logs those under
    their callback's name. What is asserted is that the subscription's own
    `_push` never did: no event, no error from it.
    """
    client = await hass_ws_client(hass)
    await subscribe(client)

    monkeypatch.delattr(entry, "runtime_data")
    async_dispatcher_send(hass, engine_state_signal(entry.entry_id))
    await hass.async_block_till_done()
    # The fetch sentinel is refused too — same predicate — so drain by hand.
    await client.send_json_auto_id({"type": WS_TYPE_STATE_GET})
    message = await client.receive_json()
    assert message["success"] is False
    assert message["error"]["code"] == "not_loaded"
    monkeypatch.undo()

    assert await pushed_documents(client) == []
    assert not [
        record
        for record in caplog.records
        if record.levelno >= logging.ERROR and "_push" in record.getMessage()
    ]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_health_view_is_json_primitives_whatever_the_context_holds(
    hass: HomeAssistant,
    entry: MockConfigEntry,
) -> None:
    """`last.context` is copied verbatim, so every leaf must already be a primitive.

    HA's socket encoder would silently coerce a `datetime`; the guard is that
    nothing but `str`/`int`/`float`/`bool`/`None` ever reaches the view.
    """
    anomalies = entry.runtime_data.anomalies
    anomalies.report(
        AnomalyKind.VALVE_OPEN_UNCONFIRMED,
        {
            "cycle_id": "2026-07-31-morning",
            "zone_id": "zone-a",
            "entity_id": VALVE_1,
            "kind": "morning",
        },
    )
    anomalies.report(
        AnomalyKind.CYCLE_RECOVERED,
        {"cycle_id": "2026-07-31-morning", "kind": "morning", "outcome": "resumed"},
    )
    await hass.async_block_till_done()

    health = health_view(anomalies)

    assert len(health["open"]) == 2
    assert health["last"] is not None
    assert_json_leaves(health)
    assert json.loads(json.dumps(health)) == health

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_disconnecting_releases_the_dispatcher_connection(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    entry: MockConfigEntry,
) -> None:
    """Matrix "Disconnect": the unsubscribe in `connection.subscriptions` runs."""
    client = await hass_ws_client(hass)
    before = dispatcher_targets(hass, entry.entry_id)

    await subscribe(client)
    assert dispatcher_targets(hass, entry.entry_id) == before + 1

    await client.close()
    await hass.async_block_till_done()

    assert dispatcher_targets(hass, entry.entry_id) == before

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_run_now_then_the_waived_scheduled_cycle_ride_the_socket(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    freezer: FrozenDateTimeFactory,
    entry: MockConfigEntry,
) -> None:
    """End to end through the socket: manual run, the deferred kind, the waiver row.

    The run-now at 06:59 is still running when the 07:00 morning falls due,
    so the scheduled cycle is DEFERRED (visible in `controller.deferred`),
    and when the run-now completes at 07:19 its day credit waives the pop in
    the same step — one history row, `waived`, naming the run-now.
    """
    calls = register_switch_domain(hass)
    client = await hass_ws_client(hass)
    await subscribe(client)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_RUN_NOW,
        {ATTR_CYCLE: "morning"},
        blocking=True,
    )
    await hass.async_block_till_done()
    started = await last_document(client)
    current = started["runs"]["current"]
    assert current is not None
    assert current["manual"] is True
    assert current["pump_entity_id"] == PUMP
    assert calls[0].data["entity_id"] == PUMP

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    deferred = await last_document(client)
    assert deferred["controller"]["deferred"] == ["morning"]
    assert deferred["runs"]["current"] is not None

    await fire_at(hass, freezer, "2026-07-31 07:09:00+02:00")
    boundary = await last_document(client)
    current = boundary["runs"]["current"]
    assert current is not None
    assert current["live_zone_id"] == "zone-b"

    await fire_at(hass, freezer, "2026-07-31 07:19:00+02:00")
    settled = await last_document(client)
    assert settled["runs"]["current"] is None
    assert settled["controller"]["deferred"] == []
    assert settled["ledger"]["day_credit"] is None
    (row,) = settled["history"]
    assert row["outcome"] == "waived"
    assert row["waived_by"] == "2026-07-31-morning"
    assert row["cycle_id"] == "2026-07-31-morning-2"
    assert row["runs"] == 2

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
