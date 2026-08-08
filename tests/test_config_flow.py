"""Config and reauth flow."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hostkeeper.api import (
    HostKeeperAuthError,
    HostKeeperConnectionError,
)
from custom_components.hostkeeper.const import (
    CONF_BASE_URL,
    CONF_PROPERTY_ID,
    CONF_PROPERTY_NAME,
    DEFAULT_BASE_URL,
    DOMAIN,
)

ECO_HAUTE = {"id": "prop-eco", "name": "Eco Haute"}
CABIN = {"id": "prop-cabin", "name": "Elena Cabin 1"}

_LIST = "custom_components.hostkeeper.config_flow.HostKeeperClient.list_properties"


async def _start(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def test_single_property_skips_the_picker(hass: HomeAssistant) -> None:
    """A key pinned to one property should not ask a question with one answer."""
    result = await _start(hass)

    with patch(_LIST, return_value=[ECO_HAUTE]):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: "hk_live_abc"}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Eco Haute"
    assert result["data"][CONF_PROPERTY_ID] == "prop-eco"
    assert result["data"][CONF_PROPERTY_NAME] == "Eco Haute"
    assert result["data"][CONF_BASE_URL] == DEFAULT_BASE_URL


async def test_multiple_properties_asks(hass: HomeAssistant) -> None:
    result = await _start(hass)

    with patch(_LIST, return_value=[ECO_HAUTE, CABIN]):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: "hk_live_abc"}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "property"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PROPERTY_ID: "prop-cabin"}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PROPERTY_ID] == "prop-cabin"


async def test_invalid_key(hass: HomeAssistant) -> None:
    result = await _start(hass)

    with patch(_LIST, side_effect=HostKeeperAuthError("revoked")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: "hk_live_bad"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_unreachable(hass: HomeAssistant) -> None:
    result = await _start(hass)

    with patch(_LIST, side_effect=HostKeeperConnectionError("dns")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: "hk_live_abc"}
        )

    assert result["errors"] == {"base": "cannot_connect"}


async def test_valid_key_reaching_nothing(hass: HomeAssistant) -> None:
    """Distinct from invalid_auth — the key works, it just can't see anything.

    Told apart so the host isn't sent hunting for a typo that isn't there.
    """
    result = await _start(hass)

    with patch(_LIST, return_value=[]):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: "hk_live_abc"}
        )

    assert result["errors"] == {"base": "no_properties"}


async def test_same_property_twice_is_refused(hass: HomeAssistant) -> None:
    """Two entries for one property would double every poll and race events."""
    MockConfigEntry(
        domain=DOMAIN, unique_id="prop-eco", data={CONF_PROPERTY_ID: "prop-eco"}
    ).add_to_hass(hass)

    result = await _start(hass)
    with patch(_LIST, return_value=[ECO_HAUTE]):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: "hk_live_abc"}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_rejects_a_key_for_the_wrong_property(
    hass: HomeAssistant,
) -> None:
    """Rotating to a key pinned elsewhere would silently repoint the entry.

    It would look like it worked, then quietly stop filing alerts for the
    house this Home Assistant is actually in.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="prop-eco",
        data={
            CONF_API_KEY: "hk_live_old",
            CONF_BASE_URL: DEFAULT_BASE_URL,
            CONF_PROPERTY_ID: "prop-eco",
            CONF_PROPERTY_NAME: "Eco Haute",
        },
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    with patch(_LIST, return_value=[CABIN]):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: "hk_live_new"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "wrong_property"}


async def test_reauth_accepts_a_key_for_the_right_property(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="prop-eco",
        data={
            CONF_API_KEY: "hk_live_old",
            CONF_BASE_URL: DEFAULT_BASE_URL,
            CONF_PROPERTY_ID: "prop-eco",
            CONF_PROPERTY_NAME: "Eco Haute",
        },
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    with (
        patch(_LIST, return_value=[ECO_HAUTE]),
        patch("custom_components.hostkeeper.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: "hk_live_new"}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_API_KEY] == "hk_live_new"
