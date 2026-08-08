"""Service behaviour — what the integration actually writes back."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hostkeeper.api import (
    HostKeeperClient,
    HostKeeperConflictError,
    HostKeeperValidationError,
)
from custom_components.hostkeeper.const import (
    CONF_BASE_URL,
    CONF_PROPERTY_ID,
    CONF_PROPERTY_NAME,
    DEFAULT_BASE_URL,
    DOMAIN,
)

from .conftest import FakeClient

PROPERTY_ID = "prop-eco"
ALERT = "binary_sensor.cistern_left_low"


def _task(status: str) -> dict:
    return {
        "id": "task-1",
        "title": "Call for a water delivery",
        "status": status,
        "external_id": ALERT,
    }


async def _setup(hass: HomeAssistant, client: FakeClient) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=PROPERTY_ID,
        data={
            CONF_API_KEY: "hk_live_abc",
            CONF_BASE_URL: DEFAULT_BASE_URL,
            CONF_PROPERTY_ID: PROPERTY_ID,
            CONF_PROPERTY_NAME: "Eco Haute",
        },
    )
    entry.add_to_hass(hass)
    with patch("custom_components.hostkeeper.HostKeeperClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_resolve_cancels_an_open_task(hass: HomeAssistant) -> None:
    """A condition that clears on its own was not work someone did.

    Recording rain as a completed job corrupts the maintenance history the
    host later relies on, so an open task is cancelled rather than completed.
    """
    client = FakeClient([_task("open")])
    await _setup(hass, client)

    await hass.services.async_call(
        DOMAIN, "resolve", {"alert_key": ALERT}, blocking=True
    )

    assert ("set_status", (PROPERTY_ID, "task-1", "cancelled"), {"block_reason": None}) in client.calls
    assert "complete" not in client.names()


async def test_resolve_verifies_a_done_task(hass: HomeAssistant) -> None:
    """When the work was claimed and the condition cleared, confirm it."""
    client = FakeClient([_task("done")])
    await _setup(hass, client)

    await hass.services.async_call(
        DOMAIN, "resolve", {"alert_key": ALERT}, blocking=True
    )

    statuses = [c[1][2] for c in client.calls if c[0] == "set_status"]
    assert statuses == ["verified"]


async def test_resolve_on_an_unknown_alert_is_a_no_op(hass: HomeAssistant) -> None:
    """Heartbeats fire constantly for conditions that are false. They must be free."""
    client = FakeClient([])
    await _setup(hass, client)

    await hass.services.async_call(
        DOMAIN, "resolve", {"alert_key": "binary_sensor.never_seen"}, blocking=True
    )

    assert "set_status" not in client.names()
    assert "complete" not in client.names()


async def test_resolve_leaves_a_verified_task_alone(hass: HomeAssistant) -> None:
    """Terminal means terminal — a later heartbeat must not churn it."""
    client = FakeClient([_task("verified")])
    await _setup(hass, client)

    await hass.services.async_call(
        DOMAIN, "resolve", {"alert_key": ALERT}, blocking=True
    )

    assert "set_status" not in client.names()


async def test_block_records_the_reason(hass: HomeAssistant) -> None:
    client = FakeClient([_task("done")])
    await _setup(hass, client)

    await hass.services.async_call(
        DOMAIN,
        "block",
        {"alert_key": ALERT, "reason": "Still reads 18% two hours later."},
        blocking=True,
    )

    call = next(c for c in client.calls if c[0] == "set_status")
    assert call[1][2] == "blocked"
    assert call[2]["block_reason"] == "Still reads 18% two hours later."


# -- the client's own idempotency ------------------------------------------


@pytest.mark.parametrize(
    "conflict",
    [
        HostKeeperConflictError("409 already represents"),
        HostKeeperValidationError("422 already represents"),
    ],
    ids=["409", "422"],
)
async def test_report_falls_back_to_lookup_on_conflict(conflict: Exception) -> None:
    """A duplicate report resolves the existing task by lookup, not by parsing.

    Both status codes are covered deliberately. The server documents and
    returns 409; an earlier build returned 422. Handling only one turns every
    heartbeat re-assert into a red automation error — which is exactly what
    happened on the first live deploy, where the whole repeat loop aborted on
    the first already-open domain and the ones after it were never evaluated.
    """
    client = HostKeeperClient(session=None, base_url="https://x", api_key="k")
    existing = {"id": "task-1", "external_id": ALERT, "status": "open"}

    with (
        patch.object(HostKeeperClient, "_invoke", side_effect=conflict),
        patch.object(
            HostKeeperClient, "find_by_alert_key", AsyncMock(return_value=existing)
        ),
    ):
        task, created = await client.report(
            PROPERTY_ID, alert_key=ALERT, title="Water delivery"
        )

    assert created is False
    assert task == existing


async def test_report_reraises_a_genuine_validation_error() -> None:
    """A real bad payload must not be silently swallowed as 'already exists'."""
    client = HostKeeperClient(session=None, base_url="https://x", api_key="k")

    with (
        patch.object(
            HostKeeperClient,
            "_invoke",
            side_effect=HostKeeperValidationError("title too long"),
        ),
        patch.object(
            HostKeeperClient, "find_by_alert_key", AsyncMock(return_value=None)
        ),
        pytest.raises(HostKeeperValidationError),
    ):
        await client.report(PROPERTY_ID, alert_key=ALERT, title="x" * 999)
