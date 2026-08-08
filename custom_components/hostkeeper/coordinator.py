"""Polling coordinator — watches HostKeeper for work that has been done.

HA is the verifier in this integration. A host or property manager marking a
task done in HostKeeper lands it at ``done``, which is deliberately not a
terminal state. This coordinator notices that transition and fires
``hostkeeper_task_completed`` on the HA event bus; an automation then does the
local half of the job (clear an ack, run a "mark changed" script, re-read the
sensor) and moves the task on to ``verified`` or ``blocked``.

Polling rather than webhooks is deliberate: it needs no inbound exposure on the
Home Assistant host, which for a box in a machine room on a domestic connection
is the difference between "works" and "requires a tunnel".
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import HostKeeperAuthError, HostKeeperClient, HostKeeperError
from .const import (
    ATTR_ALERT_KEY,
    ATTR_TASK_ID,
    ATTR_TITLE,
    DOMAIN,
    EVENT_TASK_COMPLETED,
    OPEN_STATUSES,
    STATUS_DONE,
)

_LOGGER = logging.getLogger(__name__)


class HostKeeperCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Keeps a view of this property's integration-filed tasks, keyed by alert."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: HostKeeperClient,
        property_id: str,
        property_name: str,
        scan_interval: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} ({property_name})",
            update_interval=timedelta(seconds=scan_interval),
        )
        self.client = client
        self.property_id = property_id
        self.property_name = property_name

        # alert_key -> last status we saw. Used purely for edge detection.
        self._last_status: dict[str, str] = {}
        self._primed = False

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        try:
            tasks = await self.client.list_tasks(self.property_id)
        except HostKeeperAuthError as err:
            # Surfacing this as ConfigEntryAuthFailed would be wrong here —
            # __init__ translates it, so the reauth flow starts once rather
            # than on every poll.
            raise UpdateFailed(f"authentication failed: {err}") from err
        except HostKeeperError as err:
            raise UpdateFailed(str(err)) from err

        by_key: dict[str, dict[str, Any]] = {}
        for task in tasks:
            alert_key = task.get("external_id")
            if alert_key:
                by_key[alert_key] = task

        self._detect_completions(by_key)
        return by_key

    def _detect_completions(self, by_key: dict[str, dict[str, Any]]) -> None:
        """Fire the completion event for tasks that just reached ``done``.

        The first refresh only seeds the status map. Without that guard, every
        HA restart would replay a completion event for each task already
        sitting at ``done``, which would re-run "mark the filter changed" on
        every reboot — the exact class of bug that makes an integration
        untrustworthy.
        """
        for alert_key, task in by_key.items():
            status = task.get("status")
            previous = self._last_status.get(alert_key)

            if self._primed and status == STATUS_DONE and previous != STATUS_DONE:
                # info, not debug: a task being completed is the one event
                # worth seeing in a log without turning on debug for everything.
                _LOGGER.info(
                    "task for %s reached done (task %s) — firing %s",
                    alert_key,
                    task.get("id"),
                    EVENT_TASK_COMPLETED,
                )
                self.hass.bus.async_fire(
                    EVENT_TASK_COMPLETED,
                    {
                        ATTR_ALERT_KEY: alert_key,
                        ATTR_TASK_ID: task.get("id"),
                        ATTR_TITLE: task.get("title"),
                        "property_id": self.property_id,
                    },
                )

            if status is not None:
                self._last_status[alert_key] = status

        # Forget alerts that have left the corpus entirely, so a key that is
        # re-filed months later is treated as new rather than as a stale edge.
        for gone in set(self._last_status) - set(by_key):
            del self._last_status[gone]

        self._primed = True

    @property
    def open_tasks(self) -> list[dict[str, Any]]:
        """Tasks still representing live work, for the to-do mirror."""
        return [
            task
            for task in (self.data or {}).values()
            if task.get("status") in OPEN_STATUSES
        ]
