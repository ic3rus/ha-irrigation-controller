"""Notify port and its Home Assistant adapter (Story 3.1, AD-9).

The anomaly manager is the ONLY caller of `notify` in this integration, and
it reaches it through this Protocol so the push is a seam like every other
side effect: a recording fake in tests, `notify.send_message` on a configured
`notify.*` entity in production.

Best-effort by contract: a push that fails is a WARNING in the log and
nothing more — the manager catches whatever the adapter raises, so a missing
service, a removed target or a flaky companion app can never raise an
anomaly about an anomaly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Protocol

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import valid_entity_id

from ..const import CONF_NOTIFY_TARGET, LOGGER  # noqa: TID252

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.core import HomeAssistant

# Literals rather than `homeassistant.components.notify` imports: the
# integration declares no dependency on the notify component (it only calls
# its service, which is registered when any notify entity exists), and
# hassfest would otherwise want one.
NOTIFY_DOMAIN: Final = "notify"
SERVICE_SEND_MESSAGE: Final = "send_message"
ATTR_TITLE: Final = "title"
ATTR_MESSAGE: Final = "message"


class NotifyPort(Protocol):
    """Push seam: deliver one titled message to the operator."""

    async def async_send(self, title: str, message: str) -> None:
        """Deliver `message` under `title`; raise on failure (the caller logs)."""
        ...


def parse_notify_target(options: Mapping[str, object]) -> str | None:
    """Return the configured `notify.*` entity id, or None with one WARNING.

    Fail-wet on the configuration (AD-4): a stored entry is unvalidated
    input, and neither an absent key nor a non-string, a non-entity-id or an
    entity of another domain may fail the setup — every one of them means
    "no push", said once in the log so the operator knows anomalies stay on
    the Repairs dashboard only.
    """
    value = options.get(CONF_NOTIFY_TARGET)
    if value is None:
        LOGGER.warning(
            "No notify target is configured; anomalies are recorded as Repairs "
            "issues and bus events but are not pushed",
        )
        return None
    if (
        isinstance(value, str)
        and valid_entity_id(value)
        and value.split(".", 1)[0] == NOTIFY_DOMAIN
    ):
        return value
    LOGGER.warning(
        "Ignoring the configured notify target %r: not a notify entity id; "
        "anomalies will not be pushed",
        value,
    )
    return None


class HaNotifyAdapter:
    """`NotifyPort` over `notify.send_message` on one notify entity."""

    def __init__(self, hass: HomeAssistant, entity_id: str) -> None:
        """Bind the adapter to hass and the target entity."""
        self._hass = hass
        self._entity_id = entity_id

    @property
    def entity_id(self) -> str:
        """Return the notify entity this adapter pushes to."""
        return self._entity_id

    async def async_send(self, title: str, message: str) -> None:
        """Call `notify.send_message` on the target, blocking until it returns.

        `blocking=True` so a failure surfaces HERE (as `ServiceNotFound`,
        `ServiceValidationError` or the entity's own error) where the manager
        turns it into a WARNING, rather than in a detached task that only
        logs a traceback.
        """
        await self._hass.services.async_call(
            NOTIFY_DOMAIN,
            SERVICE_SEND_MESSAGE,
            {
                ATTR_ENTITY_ID: self._entity_id,
                ATTR_TITLE: title,
                ATTR_MESSAGE: message,
            },
            blocking=True,
        )
