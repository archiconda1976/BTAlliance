"""Binary sensor platform for BTAlliance mesh infrastructure devices."""

from __future__ import annotations

import logging
import time

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, CONF_MESH_NAME, BROADCAST_ADDRESS
from .coordinator import BTAllianceMeshCoordinator

_LOGGER = logging.getLogger(__name__)

INFRASTRUCTURE_ONLINE_WINDOW = 300


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up BTAlliance infrastructure diagnostic entities."""
    coordinator: BTAllianceMeshCoordinator = hass.data[DOMAIN][entry.entry_id]
    mesh_name = entry.options.get(CONF_MESH_NAME, entry.data.get(CONF_MESH_NAME, "Fulife"))
    known_addresses: set[int] = set()
    entities: list[BinarySensorEntity] = [
        BTAllianceMeshHubDiagnostics(
            coordinator=coordinator,
            mesh_name=mesh_name,
            entry_id=entry.entry_id,
        )
    ]

    def add_infrastructure_node(mesh_addr: int) -> None:
        """Add a diagnostic entity for a mesh infrastructure device."""
        if mesh_addr in known_addresses:
            return

        known_addresses.add(mesh_addr)
        _LOGGER.info("Adding infrastructure entity for mesh address %d", mesh_addr)
        async_add_entities([
            BTAllianceMeshInfrastructureNode(
                coordinator=coordinator,
                mesh_addr=mesh_addr,
                mesh_name=mesh_name,
                entry_id=entry.entry_id,
            )
        ])

    coordinator.set_new_infrastructure_callback(add_infrastructure_node)

    for mesh_addr in sorted(coordinator.infrastructure_mesh_addresses):
        if mesh_addr not in known_addresses:
            known_addresses.add(mesh_addr)
            _LOGGER.info("Adding infrastructure entity for mesh address %d", mesh_addr)
            entities.append(
                BTAllianceMeshInfrastructureNode(
                    coordinator=coordinator,
                    mesh_addr=mesh_addr,
                    mesh_name=mesh_name,
                    entry_id=entry.entry_id,
                )
            )

    async_add_entities(entities)


class BTAllianceMeshHubDiagnostics(CoordinatorEntity, BinarySensorEntity):
    """Representation of the selected BTAlliance BLE gateway."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: BTAllianceMeshCoordinator,
        mesh_name: str,
        entry_id: str,
    ) -> None:
        """Initialize the hub diagnostic entity."""
        super().__init__(coordinator)

        self._attr_unique_id = f"{entry_id}_gateway_connection"
        self._attr_name = "Gateway Connection"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=f"{mesh_name} Mesh Hub",
            manufacturer="BTAlliance/Fulife",
            model="Telink BLE Mesh Gateway",
        )

        coordinator.register_state_callback(BROADCAST_ADDRESS, self._handle_state_update)

    async def async_will_remove_from_hass(self) -> None:
        """Handle removal from hass."""
        self.coordinator.unregister_state_callback(BROADCAST_ADDRESS, self._handle_state_update)
        await super().async_will_remove_from_hass()

    @callback
    def _handle_state_update(self) -> None:
        """Handle hub diagnostic updates."""
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        """Return whether the selected gateway is connected and logged in."""
        diagnostics = self.coordinator.get_gateway_diagnostics()
        return bool(diagnostics["connected"] and diagnostics["logged_in"])

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Return selected gateway diagnostic attributes."""
        return self.coordinator.get_gateway_diagnostics()


class BTAllianceMeshInfrastructureNode(CoordinatorEntity, BinarySensorEntity):
    """Representation of a BTAlliance mesh infrastructure node."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: BTAllianceMeshCoordinator,
        mesh_addr: int,
        mesh_name: str,
        entry_id: str,
    ) -> None:
        """Initialize the infrastructure diagnostic entity."""
        super().__init__(coordinator)

        self._mesh_addr = mesh_addr
        self._attr_unique_id = f"{entry_id}_infrastructure_{mesh_addr}"
        self._attr_name = f"Mesh Node {mesh_addr}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_infrastructure_{mesh_addr}")},
            name=f"{mesh_name} Mesh Node {mesh_addr}",
            manufacturer="BTAlliance/Fulife",
            model="Telink BLE Mesh Infrastructure Node",
            via_device=(DOMAIN, entry_id),
        )

        coordinator.register_state_callback(mesh_addr, self._handle_state_update)

    async def async_will_remove_from_hass(self) -> None:
        """Handle removal from hass."""
        self.coordinator.unregister_state_callback(self._mesh_addr, self._handle_state_update)
        await super().async_will_remove_from_hass()

    @callback
    def _handle_state_update(self) -> None:
        """Handle infrastructure state updates."""
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool | None:
        """Return whether this infrastructure node was seen recently."""
        state = self.coordinator.get_infrastructure_state(self._mesh_addr)
        if not state:
            return None

        last_seen = state.get("last_seen")
        if last_seen is None:
            return None
        return time.time() - last_seen <= INFRASTRUCTURE_ONLINE_WINDOW

    @property
    def extra_state_attributes(self) -> dict[str, int | None]:
        """Return infrastructure diagnostic attributes."""
        state = self.coordinator.get_infrastructure_state(self._mesh_addr) or {}
        last_seen = state.get("last_seen")
        return {
            "mesh_address": self._mesh_addr,
            "last_seen": int(last_seen) if last_seen else None,
        }
