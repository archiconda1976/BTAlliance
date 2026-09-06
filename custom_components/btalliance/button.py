"""Button platform for BTAlliance mesh hub controls."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, CONF_MESH_NAME
from .coordinator import BTAllianceMeshCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up BTAlliance mesh hub control buttons."""
    coordinator: BTAllianceMeshCoordinator = hass.data[DOMAIN][entry.entry_id]
    mesh_name = entry.options.get(CONF_MESH_NAME, entry.data.get(CONF_MESH_NAME, "Fulife"))

    entity_registry = er.async_get(hass)
    for key in ("broadcast_turn_on", "broadcast_turn_off"):
        entity_id = entity_registry.async_get_entity_id(
            "button",
            DOMAIN,
            f"{entry.entry_id}_{key}",
        )
        if entity_id:
            _LOGGER.info("Removing redundant BTAlliance hub button: %s", entity_id)
            entity_registry.async_remove(entity_id)

    async_add_entities([
        BTAllianceHubButton(
            coordinator=coordinator,
            entry_id=entry.entry_id,
            mesh_name=mesh_name,
            key="broadcast_query_status",
            name="Refresh Mesh Status",
            action=coordinator.async_broadcast_query_status,
            entity_category=EntityCategory.DIAGNOSTIC,
        ),
        BTAllianceHubButton(
            coordinator=coordinator,
            entry_id=entry.entry_id,
            mesh_name=mesh_name,
            key="broadcast_sync_time",
            name="Sync Mesh Time",
            action=coordinator.async_broadcast_sync_time,
            entity_category=EntityCategory.CONFIG,
        ),
    ])


class BTAllianceHubButton(CoordinatorEntity, ButtonEntity):
    """Button entity for a BTAlliance mesh hub command."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: BTAllianceMeshCoordinator,
        entry_id: str,
        mesh_name: str,
        key: str,
        name: str,
        action: Callable[[], Awaitable[bool]],
        entity_category: EntityCategory | None = None,
    ) -> None:
        """Initialize the hub button."""
        super().__init__(coordinator)

        self._action = action
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_name = name
        self._attr_entity_category = entity_category
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=f"{mesh_name} Mesh Hub",
            manufacturer="BTAlliance/Fulife",
            model="Telink BLE Mesh Gateway",
        )

    async def async_press(self) -> None:
        """Run the hub command."""
        _LOGGER.debug("Pressed BTAlliance hub button: %s", self._attr_name)
        await self._action()
