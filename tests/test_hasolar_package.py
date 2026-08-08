"""Validate the hasolar package that wires Elena's reports into HostKeeper.

Lives here because this is where the Home Assistant test harness is. The file
under test belongs to the hasolar repo; it is validated here so a YAML or
template error is caught on a laptop rather than in a plant room.
"""
from __future__ import annotations

import pathlib

import pytest
from homeassistant.components.automation.config import async_validate_config_item
from homeassistant.core import HomeAssistant
from homeassistant.util.yaml import loader as yaml_loader

PACKAGE = pathlib.Path.home() / "code/hasolar/ha/packages/hostkeeper.yaml"


@pytest.mark.skipif(not PACKAGE.exists(), reason="hasolar checkout not present")
async def test_package_automation_is_valid(hass: HomeAssistant) -> None:
    data = yaml_loader.load_yaml(str(PACKAGE))
    autos = data["automation"]
    if isinstance(autos, dict):
        autos = [autos]

    for cfg in autos:
        validated = await async_validate_config_item(hass, cfg["id"], cfg)
        assert validated is not None, cfg.get("alias")
