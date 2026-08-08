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


# -- sync: assert the whole active set -------------------------------------


async def test_sync_files_every_item(hass: HomeAssistant) -> None:
    client = FakeClient([])
    await _setup(hass, client)

    await hass.services.async_call(
        DOMAIN,
        "sync",
        {
            "items": [
                {"id": "water.filter.sediment", "text": "Sediment filter due in 7 days.",
                 "domain": "water", "due_days": 7},
                {"id": "pool.chemistry", "text": "The pool needs chemicals.",
                 "domain": "pool", "due_days": None},
            ]
        },
        blocking=True,
    )

    reported = {c[2]["alert_key"] for c in client.calls if c[0] == "report"}
    assert reported == {"water.filter.sediment", "pool.chemistry"}


async def test_sync_turns_due_days_into_a_due_date(hass: HomeAssistant) -> None:
    """A watch item with a horizon is a scheduled job, not a nag."""
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    client = FakeClient([])
    await _setup(hass, client)

    await hass.services.async_call(
        DOMAIN,
        "sync",
        {"items": [{"id": "water.filter.carbon", "text": "Carbon filter due in 7 days.",
                    "due_days": 7}]},
        blocking=True,
    )

    call = next(c for c in client.calls if c[0] == "report")
    expected = (dt_util.now().date() + timedelta(days=7)).isoformat()
    assert call[2]["due_date"] == expected


async def test_sync_closes_a_task_whose_alert_has_gone(hass: HomeAssistant) -> None:
    """The reconcile half — what makes this outage-safe.

    A key that stops being reported is an alert that cleared. It is cancelled,
    not completed: nobody did the work.
    """
    client = FakeClient([
        {"id": "task-1", "external_id": "pool.cassette", "title": "Cassette low",
         "status": "open"},
    ])
    await _setup(hass, client)

    await hass.services.async_call(
        DOMAIN,
        "sync",
        {"items": [{"id": "water.delivery", "text": "Call for water."}]},
        blocking=True,
    )

    closed = [c for c in client.calls if c[0] == "set_status"]
    assert len(closed) == 1
    assert closed[0][1][1] == "task-1"
    assert closed[0][1][2] == "cancelled"


async def test_sync_leaves_a_still_reported_alert_open(hass: HomeAssistant) -> None:
    client = FakeClient([
        {"id": "task-1", "external_id": "pool.cassette", "title": "Cassette low",
         "status": "open"},
    ])
    await _setup(hass, client)

    await hass.services.async_call(
        DOMAIN,
        "sync",
        {"items": [{"id": "pool.cassette", "text": "Cassette low (4 days left)."}]},
        blocking=True,
    )

    assert not [c for c in client.calls if c[0] == "set_status"]


async def test_sync_reconciles_even_if_one_item_fails(hass: HomeAssistant) -> None:
    """One bad item must not strand every other alert.

    This is the shape of the bug the first live deploy hit: an error mid-loop
    meant later domains were never evaluated at all.
    """
    from custom_components.hostkeeper.api import HostKeeperError

    client = FakeClient([
        {"id": "task-9", "external_id": "stale.key", "title": "Gone", "status": "open"},
    ])
    await _setup(hass, client)

    original = client.report
    calls: list[str] = []

    async def flaky(property_id, *, alert_key, **kw):
        calls.append(alert_key)
        if alert_key == "bad.one":
            raise HostKeeperError("server said no")
        return await original(property_id, alert_key=alert_key, **kw)

    client.report = flaky

    await hass.services.async_call(
        DOMAIN,
        "sync",
        {"items": [{"id": "bad.one", "text": "Explodes."},
                   {"id": "good.one", "text": "Fine."}]},
        blocking=True,
    )

    assert calls == ["bad.one", "good.one"]
    closed = [c for c in client.calls if c[0] == "set_status"]
    assert closed and closed[0][1][1] == "task-9"


async def test_sync_does_not_duplicate_a_done_task_whose_alert_persists(
    hass: HomeAssistant,
) -> None:
    """The duplicate hole, closed.

    HostKeeper's uniqueness guard covers open tasks only — a task at `done`
    does not block a second create. So an alert someone marked done while the
    condition is still true would gain a fresh task on every sync, silently,
    until the condition cleared. Verified against the live API before fixing.
    """
    client = FakeClient([
        {"id": "task-1", "external_id": "water.filter.carbon",
         "title": "Carbon filter change due in 7 days.", "status": "done"},
    ])
    await _setup(hass, client)

    await hass.services.async_call(
        DOMAIN,
        "sync",
        {"items": [{"id": "water.filter.carbon",
                    "text": "Carbon filter change due in 7 days.", "due_days": 7}]},
        blocking=True,
    )

    assert "report" not in client.names(), "must not file a second task"
    # And it must not be force-closed either — the verification loop owns it.
    assert "set_status" not in client.names()
    assert len(client.tasks) == 1


async def test_sync_prefers_title_over_text(hass: HomeAssistant) -> None:
    """`text` is the dashboard line; `title` is the job.

    "Sediment filter change due in 7 days." is wrong on a task — it duplicates
    the due date and is stale by tomorrow. The task wants "Change the sediment
    filter" with a due date beside it.
    """
    client = FakeClient([])
    await _setup(hass, client)

    await hass.services.async_call(
        DOMAIN, "sync",
        {"items": [{"id": "water.filter.sediment",
                    "text": "Sediment filter change due in 7 days.",
                    "title": "Change the sediment filter", "due_days": 7}]},
        blocking=True,
    )

    call = next(c for c in client.calls if c[0] == "report")
    assert call[2]["title"] == "Change the sediment filter"


async def test_sync_refreshes_a_stale_title(hass: HomeAssistant) -> None:
    """Retuning wording in config must reach tasks already filed.

    Titles are set at first file. Without this, every existing task keeps its
    original wording forever and only newly-filed ones get the improvement.
    """
    client = FakeClient([
        {"id": "task-1", "external_id": "water.filter.sediment",
         "title": "Sediment filter change due in 7 days.", "status": "open",
         "due_date": "2026-08-15"},
    ])
    await _setup(hass, client)

    await hass.services.async_call(
        DOMAIN, "sync",
        {"items": [{"id": "water.filter.sediment", "text": "…",
                    "title": "Change the sediment filter", "due_days": 7}]},
        blocking=True,
    )

    call = next(c for c in client.calls if c[0] == "update_fields")
    assert call[2]["title"] == "Change the sediment filter"
    assert "report" not in client.names()


async def test_sync_does_not_churn_an_unchanged_task(hass: HomeAssistant) -> None:
    """No write when nothing a human would read has changed."""
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    due = (dt_util.now().date() + timedelta(days=7)).isoformat()
    client = FakeClient([
        {"id": "task-1", "external_id": "water.filter.sediment",
         "title": "Change the sediment filter", "status": "open", "due_date": due},
    ])
    await _setup(hass, client)

    await hass.services.async_call(
        DOMAIN, "sync",
        {"items": [{"id": "water.filter.sediment", "text": "…",
                    "title": "Change the sediment filter", "due_days": 7}]},
        blocking=True,
    )

    assert "update_fields" not in client.names()
    assert "report" not in client.names()
