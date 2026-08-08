"""Thin client for the HostKeeper agent-tool API.

Every call goes through ``POST /api/v1/agent/tools/{tool}/invoke`` rather than
the REST resource routes. Both surfaces enforce API-key scopes and property
pinning, so this is a choice rather than a workaround: the tool endpoint is the
surface HostKeeper publishes to external agents (it backs the AgentSkill export
and the MCP server), it has one uniform request shape, and its filter set is
the one kept in parity with the tool catalog.

The client is deliberately dumb — no caching, no retry policy beyond what
aiohttp gives us. Scheduling belongs to the coordinator.
"""

from __future__ import annotations

import logging
from typing import Any

import asyncio

import aiohttp

from .const import SOURCE_SYSTEM

_LOGGER = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 30


class HostKeeperError(Exception):
    """Base error for all HostKeeper API failures."""


class HostKeeperAuthError(HostKeeperError):
    """The API key is missing, revoked, expired, or lacks the needed scope.

    Raised for 401 and 403 alike. The distinction matters to a human but not to
    the caller: both mean "this credential cannot do this", and both should send
    the config entry into reauth rather than be retried.
    """


class HostKeeperValidationError(HostKeeperError):
    """The server rejected the payload (422)."""


class HostKeeperConnectionError(HostKeeperError):
    """The server could not be reached."""


class HostKeeperClient:
    """Calls HostKeeper tools on behalf of one property."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        api_key: str,
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    async def _invoke(
        self,
        tool: str,
        payload: dict[str, Any] | None = None,
        property_id: str | None = None,
    ) -> Any:
        """Invoke a tool and return its ``result``.

        Raises the typed errors above rather than leaking aiohttp exceptions —
        callers distinguish "bad credential" (stop, ask the user) from
        "bad request" (a bug in our payload) from "network" (retry later).
        """
        url = f"{self._base_url}/api/v1/agent/tools/{tool}/invoke"
        body: dict[str, Any] = {"payload": payload or {}}
        if property_id is not None:
            body["property_id"] = property_id

        try:
            async with asyncio.timeout(_TIMEOUT_SECONDS):
                response = await self._session.post(
                    url,
                    json=body,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
                # Read the body before branching — error details live in it.
                try:
                    data = await response.json()
                except (aiohttp.ContentTypeError, ValueError):
                    data = {"detail": await response.text()}

                detail = data.get("detail") if isinstance(data, dict) else None

                if response.status in (401, 403):
                    raise HostKeeperAuthError(detail or f"HTTP {response.status}")
                if response.status == 422:
                    raise HostKeeperValidationError(detail or "unprocessable")
                if response.status >= 400:
                    raise HostKeeperError(f"HTTP {response.status}: {detail}")

        except (aiohttp.ClientError, TimeoutError) as err:
            raise HostKeeperConnectionError(str(err)) from err

        if isinstance(data, dict) and "result" in data:
            return data["result"]
        return data

    # -- reads ------------------------------------------------------------

    async def list_properties(self) -> list[dict[str, Any]]:
        """Properties this key may act on.

        Already filtered server-side by the key's ``allowed_property_ids``, so
        what comes back is exactly what the config flow should offer.
        """
        result = await self._invoke("properties.list")
        return result.get("properties", [])

    async def list_tasks(
        self,
        property_id: str,
        *,
        status: str | None = None,
        external_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Tasks on a property, optionally narrowed.

        Always filtered to this integration's own ``source_system`` — a host's
        hand-written tasks are none of our business and must never be closed by
        a sensor going quiet.
        """
        payload: dict[str, Any] = {"source_system": SOURCE_SYSTEM, "limit": limit}
        if status is not None:
            payload["status"] = status
        if external_id is not None:
            payload["external_id"] = external_id

        result = await self._invoke("tasks.list", payload, property_id=property_id)
        return result.get("tasks", [])

    async def find_by_alert_key(
        self, property_id: str, alert_key: str
    ) -> dict[str, Any] | None:
        """The task representing one alert, or None."""
        tasks = await self.list_tasks(property_id, external_id=alert_key, limit=1)
        return tasks[0] if tasks else None

    # -- writes -----------------------------------------------------------

    async def report(
        self,
        property_id: str,
        *,
        alert_key: str,
        title: str,
        description: str | None = None,
        task_type: str = "maintenance",
    ) -> tuple[dict[str, Any], bool]:
        """File a task for an alert. Returns ``(task, created)``.

        Idempotent by construction. HostKeeper enforces uniqueness on
        ``(property, source_system, external_id)`` among *open* tasks and
        refuses a duplicate, so re-reporting a still-active alert is safe and
        cheap — we simply look up the task that already represents it.

        We resolve the existing task with a lookup rather than by parsing the
        id out of the error prose, which would break the first time that
        sentence is reworded.
        """
        payload = {
            "title": title,
            "task_type": task_type,
            "initial_status": "open",
            "source_kind": "integration",
            "source_system": SOURCE_SYSTEM,
            "external_id": alert_key,
        }
        if description:
            payload["description"] = description

        try:
            task = await self._invoke("tasks.create", payload, property_id=property_id)
            return task, True
        except HostKeeperValidationError:
            existing = await self.find_by_alert_key(property_id, alert_key)
            if existing is None:
                # A real validation failure, not the uniqueness guard.
                raise
            return existing, False

    async def set_status(
        self,
        property_id: str,
        task_id: str,
        status: str,
        *,
        block_reason: str | None = None,
    ) -> dict[str, Any]:
        """Move a task to a status, optionally recording why it is blocked."""
        payload: dict[str, Any] = {"task_id": task_id, "status": status}
        if block_reason:
            payload["block_reason"] = block_reason
        return await self._invoke("tasks.update", payload, property_id=property_id)

    async def complete(self, property_id: str, task_id: str) -> dict[str, Any]:
        """Mark done. Lands at ``done``, which is not terminal — HA verifies."""
        return await self._invoke(
            "tasks.complete", {"task_id": task_id}, property_id=property_id
        )
