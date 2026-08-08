"""A to-do list mirroring this property's open HostKeeper tasks.

Read-mostly on purpose. HostKeeper's lifecycle has states the ``todo`` domain
cannot express — ``blocked``, ``verified``, ``parts_ordered`` — so this entity
is a view for dashboards and voice ("what's outstanding at the cabin?"), not
the place the lifecycle is driven. Ticking an item marks the task done, which
starts the same verification loop as a host marking it done in the app.
"""

from __future__ import annotations

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HostKeeperCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HostKeeperCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([HostKeeperTodoList(coordinator, entry)])


class HostKeeperTodoList(CoordinatorEntity[HostKeeperCoordinator], TodoListEntity):
    """Open tasks for one property."""

    _attr_has_entity_name = True
    _attr_name = "Tasks"
    _attr_supported_features = TodoListEntityFeature.UPDATE_TODO_ITEM

    def __init__(
        self, coordinator: HostKeeperCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.property_id}_tasks"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.property_id)},
            name=coordinator.property_name,
            manufacturer="HostKeeper",
            entry_type=None,
        )

    @property
    def todo_items(self) -> list[TodoItem]:
        return [
            TodoItem(
                uid=task["id"],
                summary=task.get("title", "Untitled task"),
                description=task.get("description"),
                due=None,
                status=TodoItemStatus.NEEDS_ACTION,
            )
            for task in self.coordinator.open_tasks
        ]

    async def async_update_todo_item(self, item: TodoItem) -> None:
        """Ticking an item marks the underlying task done in HostKeeper.

        Anything other than a completion is ignored rather than written back —
        editing a task's wording belongs in the app, where the person editing
        can see its history, vendor and photos.
        """
        if item.status != TodoItemStatus.COMPLETED or item.uid is None:
            return
        await self.coordinator.client.complete(
            self.coordinator.property_id, item.uid
        )
        await self.coordinator.async_request_refresh()
