"""Diagnostics, with the credential redacted.

Downloaded diagnostics get pasted into issues and forum posts. The API key
carries real authority over a live property, so it is redacted here rather than
trusted to anyone's judgement in the moment.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import HostKeeperCoordinator

TO_REDACT = {CONF_API_KEY}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    coordinator: HostKeeperCoordinator = hass.data[DOMAIN][entry.entry_id]
    tasks = coordinator.data or {}

    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "last_update_success": coordinator.last_update_success,
        "task_count": len(tasks),
        "open_task_count": len(coordinator.open_tasks),
        # Alert keys and statuses only. Titles can carry a guest's name or a
        # host's phone number, so they stay out of a shareable file.
        "alerts": {
            alert_key: task.get("status") for alert_key, task in tasks.items()
        },
    }
