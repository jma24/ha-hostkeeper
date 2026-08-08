"""Config and reauth flow for HostKeeper.

The API key is validated by actually calling ``properties.list``, which does
double duty: it proves the credential works, and what comes back is already
filtered to the properties the key is pinned to — so the picker can only offer
choices that will subsequently work.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_API_KEY, CONF_SCAN_INTERVAL
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .api import (
    HostKeeperAuthError,
    HostKeeperClient,
    HostKeeperConnectionError,
    HostKeeperError,
)
from .const import (
    CONF_BASE_URL,
    CONF_PROPERTY_ID,
    CONF_PROPERTY_NAME,
    DEFAULT_BASE_URL,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class HostKeeperConfigFlow(ConfigFlow, domain=DOMAIN):
    """Walks the host from an API key to one configured property."""

    VERSION = 1

    def __init__(self) -> None:
        self._api_key: str | None = None
        self._base_url: str = DEFAULT_BASE_URL
        self._properties: list[dict[str, Any]] = []

    async def _validate(self, api_key: str, base_url: str) -> list[dict[str, Any]]:
        client = HostKeeperClient(
            async_get_clientsession(self.hass), base_url, api_key
        )
        return await client.list_properties()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            base_url = user_input.get(CONF_BASE_URL, DEFAULT_BASE_URL).strip()

            try:
                properties = await self._validate(api_key, base_url)
            except HostKeeperAuthError:
                errors["base"] = "invalid_auth"
            except HostKeeperConnectionError:
                errors["base"] = "cannot_connect"
            except HostKeeperError:
                _LOGGER.exception("unexpected error validating HostKeeper key")
                errors["base"] = "unknown"
            else:
                if not properties:
                    # A valid key that can reach nothing — almost always a key
                    # pinned to a property the minting user later lost access
                    # to. Distinct message; "invalid_auth" would send the host
                    # hunting for a typo that isn't there.
                    errors["base"] = "no_properties"
                else:
                    self._api_key = api_key
                    self._base_url = base_url
                    self._properties = properties
                    if len(properties) == 1:
                        return await self._create(properties[0])
                    return await self.async_step_property()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_API_KEY): str,
                    vol.Optional(CONF_BASE_URL, default=DEFAULT_BASE_URL): str,
                }
            ),
            errors=errors,
        )

    async def async_step_property(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick which property this entry drives, when the key reaches several."""
        if user_input is not None:
            chosen = next(
                p for p in self._properties if p["id"] == user_input[CONF_PROPERTY_ID]
            )
            return await self._create(chosen)

        return self.async_show_form(
            step_id="property",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PROPERTY_ID): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(
                                    value=p["id"], label=p.get("name", p["id"])
                                )
                                for p in self._properties
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def _create(self, prop: dict[str, Any]) -> ConfigFlowResult:
        # One entry per property. Adding a second key for the same property
        # would double every poll and race the completion events against
        # itself.
        await self.async_set_unique_id(prop["id"])
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=prop.get("name", "HostKeeper"),
            data={
                CONF_API_KEY: self._api_key,
                CONF_BASE_URL: self._base_url,
                CONF_PROPERTY_ID: prop["id"],
                CONF_PROPERTY_NAME: prop.get("name", "HostKeeper"),
                CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL_SECONDS,
            },
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Entered when a key is revoked or rotated."""
        self._base_url = entry_data.get(CONF_BASE_URL, DEFAULT_BASE_URL)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            try:
                properties = await self._validate(api_key, self._base_url)
            except HostKeeperAuthError:
                errors["base"] = "invalid_auth"
            except HostKeeperConnectionError:
                errors["base"] = "cannot_connect"
            except HostKeeperError:
                errors["base"] = "unknown"
            else:
                configured = entry.data[CONF_PROPERTY_ID]
                if not any(p["id"] == configured for p in properties):
                    # Rotating to a key pinned elsewhere would silently
                    # repoint this entry at nothing.
                    errors["base"] = "wrong_property"
                else:
                    return self.async_update_reload_and_abort(
                        entry, data_updates={CONF_API_KEY: api_key}
                    )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_API_KEY): str}),
            errors=errors,
            description_placeholders={"property": entry.data[CONF_PROPERTY_NAME]},
        )
