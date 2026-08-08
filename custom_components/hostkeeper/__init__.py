"""HostKeeper integration — file property alerts as tasks, and close the loop.

Division of responsibility, which is the whole design:

  Home Assistant  owns whether a condition is true.
  HostKeeper      owns whether work happened.

Automations report alerts by a stable key; HostKeeper holds the correlation, so
this integration keeps no local map of alerts to tasks. Re-reporting a live
alert is idempotent server-side. When a task reaches ``done``, the coordinator
fires ``hostkeeper_task_completed`` and the automation decides what "done"
means locally — verify a sensor actually cleared, or perform the change the
sensor cannot observe.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, CONF_SCAN_INTERVAL, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .api import HostKeeperAuthError, HostKeeperClient, HostKeeperError
from .const import (
    ATTR_ALERT_KEY,
    ATTR_ITEMS,
    ATTR_DESCRIPTION,
    ATTR_REASON,
    ATTR_TASK_TYPE,
    ATTR_TITLE,
    CONF_BASE_URL,
    CONF_PROPERTY_ID,
    CONF_PROPERTY_NAME,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DOMAIN,
    OPEN_STATUSES,
    SERVICE_BLOCK,
    SERVICE_REPORT,
    SERVICE_RESOLVE,
    SERVICE_SYNC,
    STATUS_BLOCKED,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_VERIFIED,
)
from .coordinator import HostKeeperCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.TODO]

_REPORT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ALERT_KEY): cv.string,
        vol.Required(ATTR_TITLE): cv.string,
        vol.Optional(ATTR_DESCRIPTION): cv.string,
        vol.Optional(ATTR_TASK_TYPE, default="maintenance"): vol.In(
            ["maintenance", "cleaning", "inspection", "other"]
        ),
        vol.Optional(CONF_PROPERTY_ID): cv.string,
    }
)

_RESOLVE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ALERT_KEY): cv.string,
        vol.Optional(CONF_PROPERTY_ID): cv.string,
    }
)

_BLOCK_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ALERT_KEY): cv.string,
        vol.Required(ATTR_REASON): cv.string,
        vol.Optional(CONF_PROPERTY_ID): cv.string,
    }
)


_SYNC_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ITEMS): vol.All(
            cv.ensure_list,
            [
                vol.Schema(
                    {
                        vol.Required("id"): cv.string,
                        vol.Required("text"): cv.string,
                        vol.Optional("domain"): vol.Any(cv.string, None),
                        vol.Optional("due_days"): vol.Any(vol.Coerce(int), None),
                    },
                    extra=vol.ALLOW_EXTRA,
                )
            ],
        ),
        vol.Optional(CONF_PROPERTY_ID): cv.string,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one property."""
    client = HostKeeperClient(
        async_get_clientsession(hass),
        entry.data[CONF_BASE_URL],
        entry.data[CONF_API_KEY],
    )
    coordinator = HostKeeperCoordinator(
        hass,
        client,
        property_id=entry.data[CONF_PROPERTY_ID],
        property_name=entry.data[CONF_PROPERTY_NAME],
        scan_interval=entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_SECONDS),
    )

    try:
        await coordinator.async_config_entry_first_refresh()
    except HostKeeperAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _async_register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN]:
            for service in (SERVICE_REPORT, SERVICE_RESOLVE, SERVICE_BLOCK, SERVICE_SYNC):
                hass.services.async_remove(DOMAIN, service)
    return unloaded


def _resolve_coordinator(
    hass: HomeAssistant, property_id: str | None
) -> HostKeeperCoordinator:
    """Pick which property a service call is about.

    With a single configured property — the common case — the caller can omit
    ``property_id`` entirely, which keeps blueprints readable. With several,
    guessing would silently file alerts against the wrong house, so we refuse.
    """
    coordinators: dict[str, HostKeeperCoordinator] = hass.data.get(DOMAIN, {})
    if not coordinators:
        raise HomeAssistantError("No HostKeeper property is configured")

    if property_id is not None:
        for coordinator in coordinators.values():
            if coordinator.property_id == property_id:
                return coordinator
        raise HomeAssistantError(f"No configured HostKeeper property {property_id}")

    if len(coordinators) > 1:
        raise HomeAssistantError(
            "Several HostKeeper properties are configured — pass property_id"
        )
    return next(iter(coordinators.values()))


async def _resolve_task(
    coordinator: HostKeeperCoordinator, task: dict[str, Any]
) -> None:
    """Close one task, picking the transition its current state allows.

      done       -> verified   the completion was confirmed locally
      still open -> cancelled  it cleared on its own; nobody did anything

    Cancelled rather than completed matters. A cistern that refilled after
    rain is not a job someone did, and recording it as one corrupts the
    maintenance history the host later relies on.
    """
    status = task.get("status")
    if status == STATUS_DONE:
        target = STATUS_VERIFIED
    elif status in OPEN_STATUSES:
        target = STATUS_CANCELLED
    else:
        return
    await coordinator.client.set_status(coordinator.property_id, task["id"], target)


def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_REPORT):
        return

    async def _report(call: ServiceCall) -> None:
        coordinator = _resolve_coordinator(hass, call.data.get(CONF_PROPERTY_ID))
        try:
            task, created = await coordinator.client.report(
                coordinator.property_id,
                alert_key=call.data[ATTR_ALERT_KEY],
                title=call.data[ATTR_TITLE],
                description=call.data.get(ATTR_DESCRIPTION),
                task_type=call.data[ATTR_TASK_TYPE],
            )
        except HostKeeperError as err:
            raise HomeAssistantError(f"HostKeeper rejected the report: {err}") from err

        _LOGGER.debug(
            "%s alert %s -> task %s",
            "filed" if created else "already open",
            call.data[ATTR_ALERT_KEY],
            task.get("id"),
        )
        await coordinator.async_request_refresh()

    async def _resolve(call: ServiceCall) -> None:
        """The alert is no longer active.

        Which transition that means depends on whether work happened, and the
        blueprint author should not have to know HostKeeper's state machine:

          done      -> verified   the completion was confirmed locally
          still open -> cancelled  it cleared on its own; nobody did anything

        Cancelled rather than completed matters. A cistern that refilled after
        rain is not a job someone did, and recording it as one corrupts the
        maintenance history the host later relies on.
        """
        coordinator = _resolve_coordinator(hass, call.data.get(CONF_PROPERTY_ID))
        alert_key = call.data[ATTR_ALERT_KEY]

        task = await coordinator.client.find_by_alert_key(
            coordinator.property_id, alert_key
        )
        if task is None:
            _LOGGER.debug("resolve: no open task for %s — nothing to do", alert_key)
            return

        try:
            await _resolve_task(coordinator, task)
        except HostKeeperError as err:
            raise HomeAssistantError(f"HostKeeper rejected the resolve: {err}") from err
        await coordinator.async_request_refresh()

    async def _block(call: ServiceCall) -> None:
        """A completion was refuted — the work was attempted and did not take.

        Distinct from reopening. ``open`` says nobody has tried; ``blocked``
        with a reason says someone did and the condition persists, which is
        what the next person needs to know.
        """
        coordinator = _resolve_coordinator(hass, call.data.get(CONF_PROPERTY_ID))
        task = await coordinator.client.find_by_alert_key(
            coordinator.property_id, call.data[ATTR_ALERT_KEY]
        )
        if task is None:
            return
        try:
            await coordinator.client.set_status(
                coordinator.property_id,
                task["id"],
                STATUS_BLOCKED,
                block_reason=call.data[ATTR_REASON],
            )
        except HostKeeperError as err:
            raise HomeAssistantError(f"HostKeeper rejected the block: {err}") from err
        await coordinator.async_request_refresh()

    async def _sync(call: ServiceCall) -> None:
        """Assert the complete set of currently-active alerts.

        Reports everything in `items`, then closes any task this integration
        owns whose key is absent. That reconciliation is what makes the whole
        thing restart-safe and outage-safe: a missed state change heals on the
        next call rather than stranding a task open forever.

        **`items` is authoritative.** Anything previously filed through
        `report` and not present here will be closed. Use `report`/`resolve`
        instead if something else in your config files alerts independently —
        two producers and one `sync` will fight.
        """
        coordinator = _resolve_coordinator(hass, call.data.get(CONF_PROPERTY_ID))

        active: dict[str, dict[str, Any]] = {}
        for item in call.data[ATTR_ITEMS]:
            # Later items win: the same id appearing as both a watch and an
            # action item means it escalated, and the action wording is the
            # one worth showing.
            active[item["id"]] = item

        # Know the current state before writing anything. HostKeeper's
        # uniqueness guard only covers *open* tasks — a task sitting at `done`
        # does not block a second create — so reporting blind would file a
        # duplicate every pass for any alert someone has marked done while the
        # condition is still true. Verified empirically against the live API.
        await coordinator.async_refresh()
        known = coordinator.data or {}

        for key, item in active.items():
            existing = known.get(key)
            if existing is not None and existing.get("status") == STATUS_DONE:
                # Claimed complete, condition still true. That is the
                # verification moment, not a new job: leave it for the
                # completion action (or a refutation) to settle rather than
                # opening a second task alongside it.
                _LOGGER.debug(
                    "hostkeeper: %s is done but still reported — not duplicating", key
                )
                continue

            due_days = item.get("due_days")
            due_date = None
            if due_days is not None:
                # Negative is overdue, which lands a due date in the past —
                # correct, and what makes it sort to the top for whoever
                # picks the work up.
                due_date = (
                    dt_util.now().date() + timedelta(days=int(due_days))
                ).isoformat()
            try:
                await coordinator.client.report(
                    coordinator.property_id,
                    alert_key=key,
                    title=item["text"],
                    description=item.get("description"),
                    due_date=due_date,
                )
            except HostKeeperError as err:
                # One bad item must not stop the rest being filed, nor the
                # reconciliation below from running.
                _LOGGER.warning("hostkeeper: could not report %s: %s", key, err)

        await coordinator.async_refresh()

        for alert_key, task in (coordinator.data or {}).items():
            if alert_key in active:
                continue
            try:
                await _resolve_task(coordinator, task)
            except HostKeeperError as err:
                _LOGGER.warning("hostkeeper: could not close %s: %s", alert_key, err)

        await coordinator.async_request_refresh()

    hass.services.async_register(DOMAIN, SERVICE_REPORT, _report, schema=_REPORT_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_SYNC, _sync, schema=_SYNC_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_RESOLVE, _resolve, schema=_RESOLVE_SCHEMA
    )
    hass.services.async_register(DOMAIN, SERVICE_BLOCK, _block, schema=_BLOCK_SCHEMA)
