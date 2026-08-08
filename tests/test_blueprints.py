"""The blueprints must load, and must produce a valid automation once filled in.

Validating these matters more than it looks. A blueprint that fails to load is
discovered by a human reading a Home Assistant log — which, if the machine is in
a plant room on another island, is an expensive way to find a typo.

Everything here runs inside a `hass` instance: template validation is refused
outside the event loop, so a standalone schema check silently skips the part
most likely to be wrong.
"""

from __future__ import annotations

import pathlib

import pytest
from homeassistant.components.automation.config import (
    AUTOMATION_BLUEPRINT_SCHEMA,
    async_validate_config_item,
)
from homeassistant.components.blueprint.models import Blueprint, BlueprintInputs
from homeassistant.core import HomeAssistant
from homeassistant.util.yaml import loader as yaml_loader

BLUEPRINT_DIR = pathlib.Path(__file__).parent.parent / "blueprints/automation/hostkeeper"

SENSOR_ALERT = BLUEPRINT_DIR / "sensor_alert.yaml"
MANUAL_TASK = BLUEPRINT_DIR / "manual_task.yaml"

SENSOR_ALERT_INPUTS = {
    "alert_entity": "binary_sensor.cistern_left_low",
    "alert_key": "binary_sensor.cistern_left_low",
    "task_title": "Call for a water delivery",
    "task_description": "",
    "grace_minutes": 15,
    "heartbeat_minutes": 30,
}

MANUAL_TASK_INPUTS = {
    "condition_entity": "binary_sensor.sediment_filter_due",
    "alert_key": "filter.sediment",
    "task_title": "Change the sediment filter",
    "task_description": "",
    "completion_action": [
        {"service": "script.filter_sediment_mark_changed", "data": {}}
    ],
    "heartbeat_minutes": 30,
}


def _substitute(blueprint: Blueprint, inputs: dict) -> dict:
    """Fill a blueprint in, the way Home Assistant does when you create one."""
    holder = BlueprintInputs(blueprint, {"use_blueprint": {"input": inputs}})
    holder.validate()
    return holder.async_substitute()


def _load(path: pathlib.Path) -> Blueprint:
    return Blueprint(
        yaml_loader.load_yaml(str(path)),
        expected_domain="automation",
        schema=AUTOMATION_BLUEPRINT_SCHEMA,
        path=str(path),
    )


@pytest.mark.parametrize("path", [SENSOR_ALERT, MANUAL_TASK], ids=lambda p: p.name)
async def test_blueprint_loads(hass: HomeAssistant, path: pathlib.Path) -> None:
    blueprint = _load(path)
    assert blueprint.domain == "automation"
    assert blueprint.name
    assert blueprint.inputs


async def test_sensor_alert_declares_the_inputs_the_readme_promises(
    hass: HomeAssistant,
) -> None:
    assert set(SENSOR_ALERT_INPUTS) <= set(_load(SENSOR_ALERT).inputs)


async def test_manual_task_exposes_an_action_input(hass: HomeAssistant) -> None:
    """The action input is the whole extensibility story.

    It is how a host binds `script.filter_sediment_mark_changed` without this
    integration ever learning that entity exists. If the selector regresses to
    something narrower, the design breaks quietly.
    """
    blueprint = _load(MANUAL_TASK)
    assert "completion_action" in blueprint.inputs
    assert "action" in blueprint.inputs["completion_action"]["selector"]


@pytest.mark.parametrize(
    ("path", "inputs"),
    [(SENSOR_ALERT, SENSOR_ALERT_INPUTS), (MANUAL_TASK, MANUAL_TASK_INPUTS)],
    ids=["sensor_alert", "manual_task"],
)
async def test_filled_in_blueprint_is_a_valid_automation(
    hass: HomeAssistant, path: pathlib.Path, inputs: dict
) -> None:
    """Substitute real inputs and validate the automation HA would actually run."""
    config = _substitute(_load(path), inputs)
    config["id"] = path.stem

    validated = await async_validate_config_item(hass, path.stem, config)
    assert validated is not None


async def test_manual_task_confirms_before_the_condition_can_clear(
    hass: HomeAssistant,
) -> None:
    """Ordering guard: actuate, then confirm, and only then let reconcile run.

    The completion action clears the condition. If `resolve` did not run first,
    the next pass would see a cleared condition, read it as "resolved on its
    own", and file real maintenance as cancelled — quietly recording that
    nobody changed the filter.
    """
    config = _substitute(_load(MANUAL_TASK), MANUAL_TASK_INPUTS)
    # Home Assistant normalises `action:` to `actions:` on substitution.
    completed_branch = config["actions"][0]["choose"][0]["sequence"]

    # The nominated action runs first...
    assert completed_branch[0]["choose"][0]["sequence"] == (
        MANUAL_TASK_INPUTS["completion_action"]
    )
    # ...and the confirmation immediately after, in the same sequence.
    assert completed_branch[1]["service"] == "hostkeeper.resolve"
