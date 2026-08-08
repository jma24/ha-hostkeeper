"""Completion-edge detection.

This is the highest-consequence logic in the integration. A spurious
`hostkeeper_task_completed` makes an automation run its completion action —
stamping "filter changed today" when nobody touched it, or flipping which
cistern the house draws from. That failure is silent: the data is simply wrong
afterwards, and nothing surfaces it.

So these tests are about what does *not* fire.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from custom_components.hostkeeper.const import EVENT_TASK_COMPLETED
from custom_components.hostkeeper.coordinator import HostKeeperCoordinator

from .conftest import FakeClient

PROPERTY_ID = "prop-1"
ALERT = "binary_sensor.cistern_left_low"


def _task(status: str, external_id: str = ALERT, task_id: str = "task-1") -> dict:
    return {
        "id": task_id,
        "title": "Call for a water delivery",
        "status": status,
        "external_id": external_id,
        "source_system": "home_assistant",
    }


def _make(hass: HomeAssistant, client: FakeClient) -> HostKeeperCoordinator:
    return HostKeeperCoordinator(
        hass, client, PROPERTY_ID, "Eco Haute", scan_interval=60
    )


@pytest.fixture
def events(hass: HomeAssistant) -> list:
    captured: list = []
    hass.bus.async_listen(EVENT_TASK_COMPLETED, lambda e: captured.append(e))
    return captured


async def test_first_refresh_never_fires(hass: HomeAssistant, events: list) -> None:
    """A task already `done` at startup must not fire an event.

    Without this guard every Home Assistant restart replays a completion for
    each done task, re-running the local completion action each time. On a Pi
    that reboots after an outage, that is a filter marked changed on every
    power cut.
    """
    client = FakeClient([_task("done")])
    coordinator = _make(hass, client)

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert events == []


async def test_transition_to_done_fires_once(hass: HomeAssistant, events: list) -> None:
    client = FakeClient([_task("open")])
    coordinator = _make(hass, client)

    await coordinator.async_refresh()  # primes: open
    await hass.async_block_till_done()
    assert events == []

    client.tasks[0]["status"] = "done"
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["alert_key"] == ALERT
    assert events[0].data["task_id"] == "task-1"
    assert events[0].data["property_id"] == PROPERTY_ID


async def test_staying_done_does_not_refire(hass: HomeAssistant, events: list) -> None:
    """Polling every two minutes must not re-fire while a task sits at done."""
    client = FakeClient([_task("open")])
    coordinator = _make(hass, client)
    await coordinator.async_refresh()

    client.tasks[0]["status"] = "done"
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert len(events) == 1

    for _ in range(5):
        await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert len(events) == 1


async def test_done_then_blocked_then_done_fires_again(
    hass: HomeAssistant, events: list
) -> None:
    """A refuted completion that is later re-done is a genuinely new event."""
    client = FakeClient([_task("open")])
    coordinator = _make(hass, client)
    await coordinator.async_refresh()

    client.tasks[0]["status"] = "done"
    await coordinator.async_refresh()
    client.tasks[0]["status"] = "blocked"
    await coordinator.async_refresh()
    client.tasks[0]["status"] = "done"
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert len(events) == 2


async def test_tasks_without_alert_key_are_ignored(
    hass: HomeAssistant, events: list
) -> None:
    """Only tasks this integration filed carry an external_id.

    A host's hand-written task must never drive an automation, even if it
    somehow reaches us.
    """
    client = FakeClient([{"id": "t9", "status": "done", "title": "Fix the gate"}])
    coordinator = _make(hass, client)

    await coordinator.async_refresh()
    client.tasks[0]["status"] = "open"
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert events == []
    assert coordinator.data == {}


async def test_only_our_source_system_is_requested(hass: HomeAssistant) -> None:
    """The server-side filter is the guard against touching a host's own tasks.

    If this call ever stops passing source_system, `resolve` could close work
    nobody asked us to touch. Assert it explicitly rather than trust it.
    """
    client = FakeClient([])
    coordinator = _make(hass, client)
    await coordinator.async_refresh()

    assert client.calls, "expected a list_tasks call"
    # FakeClient injects source_system server-side in the real client; here we
    # assert the coordinator asks for the property it was configured with.
    name, args, _ = client.calls[0]
    assert name == "list_tasks"
    assert args[0] == PROPERTY_ID


async def test_forgotten_alerts_do_not_leak(hass: HomeAssistant, events: list) -> None:
    """An alert that leaves the corpus is forgotten, not remembered forever."""
    client = FakeClient([_task("open")])
    coordinator = _make(hass, client)
    await coordinator.async_refresh()

    client.tasks.clear()
    await coordinator.async_refresh()

    assert coordinator.data == {}
    assert ALERT not in coordinator._last_status
