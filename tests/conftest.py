"""Shared fixtures."""

from __future__ import annotations

from typing import Any

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load `custom_components/hostkeeper` during tests."""
    yield


class FakeClient:
    """Stands in for HostKeeperClient.

    Records calls so tests can assert on what the integration *did*, not just
    what it returned — most of the risk here is in spurious writes.
    """

    def __init__(self, tasks: list[dict[str, Any]] | None = None) -> None:
        self.tasks = tasks or []
        self.calls: list[tuple[str, tuple, dict]] = []

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    async def list_tasks(self, property_id: str, **kwargs: Any) -> list[dict]:
        self._record("list_tasks", property_id, **kwargs)
        external_id = kwargs.get("external_id")
        if external_id is not None:
            return [t for t in self.tasks if t.get("external_id") == external_id]
        return list(self.tasks)

    async def find_by_alert_key(self, property_id: str, alert_key: str):
        found = await self.list_tasks(property_id, external_id=alert_key)
        return found[0] if found else None

    async def set_status(self, property_id, task_id, status, *, block_reason=None):
        self._record("set_status", property_id, task_id, status, block_reason=block_reason)
        for task in self.tasks:
            if task["id"] == task_id:
                task["status"] = status
        return {"id": task_id, "status": status}

    async def complete(self, property_id: str, task_id: str):
        self._record("complete", property_id, task_id)
        return {"id": task_id, "status": "done"}

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient()
