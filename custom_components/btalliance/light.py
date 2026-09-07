"""Light platform for BTAlliance Mesh Lights."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, CONF_MESH_NAME, BROADCAST_ADDRESS, MAX_CONNECTION_RETRIES
from .coordinator import BTAllianceMeshCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up BTAlliance lights from a config entry."""
    coordinator: BTAllianceMeshCoordinator = hass.data[DOMAIN][entry.entry_id]
    mesh_name = entry.data.get(CONF_MESH_NAME, "Fulife")
    device_registry = dr.async_get(hass)
    hub_device = device_registry.async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    hub_device_id = hub_device.id if hub_device else None
    
    # Track which mesh addresses have entities
    known_addresses: set[int] = set()

    entity_registry = er.async_get(hass)
    for registry_entry in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
        if registry_entry.domain != "light":
            continue

        unique_id_prefix = f"{entry.entry_id}_"
        if not registry_entry.unique_id.startswith(unique_id_prefix):
            continue

        raw_mesh_addr = registry_entry.unique_id.removeprefix(unique_id_prefix)
        try:
            mesh_addr = int(raw_mesh_addr)
        except ValueError:
            continue

        coordinator.add_cached_light_address(mesh_addr)
    
    def add_new_device(mesh_addr: int) -> None:
        """Add a new light entity for a discovered mesh device."""
        if coordinator.is_infrastructure_address(mesh_addr):
            _LOGGER.info(
                "Mesh address %d is configured as infrastructure; not adding a light entity",
                mesh_addr,
            )
            return

        if mesh_addr in known_addresses:
            return
        
        known_addresses.add(mesh_addr)
        _LOGGER.info("Adding new light entity for mesh address %d", mesh_addr)
        
        async_add_entities([
            BTAllianceMeshLight(
                coordinator=coordinator,
                mesh_addr=mesh_addr,
                mesh_name=mesh_name,
                entry_id=entry.entry_id,
                hub_device_id=hub_device_id,
            )
        ])
    
    # Register callback for dynamic device discovery BEFORE connecting
    # This way devices discovered during connection are added automatically
    coordinator.set_new_device_callback(add_new_device)

    _LOGGER.info("Adding broadcast control light entity")
    async_add_entities([
        BTAllianceBroadcastLight(
            coordinator=coordinator,
            mesh_name=mesh_name,
            entry_id=entry.entry_id,
        )
    ])

    for mesh_addr in sorted(coordinator.cached_light_mesh_addresses):
        add_new_device(mesh_addr)
    
    # Start connection and discovery in background task to avoid blocking startup
    async def connect_and_discover() -> None:
        """Connect to gateway and discover devices in background."""
        connected = False
        for attempt in range(1, MAX_CONNECTION_RETRIES + 1):
            if await coordinator.async_connect():
                connected = True
                break

            _LOGGER.warning(
                "Failed to connect to gateway (attempt %d/%d)",
                attempt,
                MAX_CONNECTION_RETRIES,
            )
            await asyncio.sleep(2.0)

        if not connected:
            _LOGGER.error("Failed to connect to gateway after retries")
            return
        
        # Short discovery to find initial devices
        _LOGGER.info("Discovering mesh devices...")
        await coordinator.async_discover_mesh_devices(timeout=3.0)

        for mesh_addr in sorted(coordinator.cached_light_mesh_addresses):
            if coordinator.is_infrastructure_address(mesh_addr):
                continue

            _LOGGER.info("Refreshing full status for cached mesh light %d", mesh_addr)
            await coordinator.async_query_status(mesh_addr)
            await asyncio.sleep(0.5)
        
        # Log what we found
        _LOGGER.info("Initial discovery complete: %d devices", len(coordinator.discovered_devices))
    
    # Schedule connection in background - don't block platform setup
    entry.async_create_background_task(
        hass,
        connect_and_discover(),
        "btalliance_connect",
    )


class BTAllianceMeshLight(CoordinatorEntity, LightEntity):
    """Representation of a BTAlliance mesh light."""

    _attr_has_entity_name = True
    _attr_supported_color_modes = {ColorMode.RGB, ColorMode.COLOR_TEMP}
    _attr_color_mode = ColorMode.RGB
    _attr_supported_features = LightEntityFeature(0)
    _attr_min_color_temp_kelvin = 2700  # warm
    _attr_max_color_temp_kelvin = 6500  # cool

    def __init__(
        self,
        coordinator: BTAllianceMeshCoordinator,
        mesh_addr: int,
        mesh_name: str,
        entry_id: str,
        hub_device_id: str | None,
    ) -> None:
        """Initialize the light."""
        super().__init__(coordinator)
        
        self._mesh_addr = mesh_addr
        self._mesh_name = mesh_name
        self._entry_id = entry_id
        
        # Entity IDs
        self._attr_unique_id = f"{entry_id}_{mesh_addr}"
        self._attr_name = f"Light {mesh_addr}"
        
        # Device info - group all lights under the mesh network
        device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_{mesh_addr}")},
            name=f"{mesh_name} Light {mesh_addr}",
            manufacturer="BTAlliance/Fulife",
            model="Telink BLE Mesh Light",
        )
        if hub_device_id:
            device_info["via_device_id"] = hub_device_id
        self._attr_device_info = device_info
        
        # Register for state updates
        coordinator.register_state_callback(mesh_addr, self._handle_state_update)
    
    async def async_will_remove_from_hass(self) -> None:
        """Handle removal from hass."""
        self.coordinator.unregister_state_callback(self._mesh_addr, self._handle_state_update)
        await super().async_will_remove_from_hass()

    @callback
    def async_removed_from_registry(self) -> None:
        """Handle manual removal from the entity registry."""
        self.coordinator.remove_cached_light_address(self._mesh_addr)
    
    @callback
    def _handle_state_update(self) -> None:
        """Handle state update from coordinator."""
        self.async_write_ha_state()
    
    @property
    def is_on(self) -> bool | None:
        """Return true if light is on."""
        state = self.coordinator.get_light_state(self._mesh_addr)
        if state:
            return state.get('is_on', False)
        return None
    
    @property
    def brightness(self) -> int | None:
        """Return the brightness of the light (0-255)."""
        state = self.coordinator.get_light_state(self._mesh_addr)
        if state and state.get('luminance') is not None:
            # Convert 0-100 to 0-255
            return int(state['luminance'] * 255 / 100)
        return None
    
    @property
    def color_mode(self) -> ColorMode | None:
        """Return the active color mode."""
        state = self.coordinator.get_light_state(self._mesh_addr)
        if state and state.get('color_mode') == 'color_temp':
            return ColorMode.COLOR_TEMP
        if state and state.get('color_mode') == 'rgb':
            return ColorMode.RGB
        return self._attr_color_mode

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        """Return the RGB color value."""
        state = self.coordinator.get_light_state(self._mesh_addr)
        if state and state.get('color_mode') != 'color_temp':
            r = state.get('red')
            g = state.get('green')
            b = state.get('blue')
            if r is not None and g is not None and b is not None:
                return (r, g, b)
        return None
    
    @property
    def color_temp_kelvin(self) -> int | None:
        """Return the color temperature in Kelvin."""
        state = self.coordinator.get_light_state(self._mesh_addr)
        if state and state.get('color_temp') is not None:
            # Convert 0-100 (warm-cool) to Kelvin
            # 0 = warm = 2700K, 100 = cool = 6500K
            ct_pct = state['color_temp']
            kelvin = 2700 + int(ct_pct * (6500 - 2700) / 100)
            return kelvin
        return None
    
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the light on."""
        _LOGGER.debug("Turn on light %d with kwargs: %s", self._mesh_addr, kwargs)
        
        # Handle brightness
        if ATTR_BRIGHTNESS in kwargs:
            brightness = kwargs[ATTR_BRIGHTNESS]
            await self.coordinator.async_set_brightness(self._mesh_addr, brightness)
        
        # Handle RGB color
        if ATTR_RGB_COLOR in kwargs:
            r, g, b = kwargs[ATTR_RGB_COLOR]
            await self.coordinator.async_set_rgb(self._mesh_addr, r, g, b)
            self._attr_color_mode = ColorMode.RGB
        
        # Handle color temperature
        elif ATTR_COLOR_TEMP_KELVIN in kwargs:
            kelvin = kwargs[ATTR_COLOR_TEMP_KELVIN]
            # Convert Kelvin to 0-100 (warm-cool)
            # 2700K = 0 (warm), 6500K = 100 (cool)
            ct_pct = int((kelvin - 2700) * 100 / (6500 - 2700))
            ct_pct = max(0, min(100, ct_pct))
            await self.coordinator.async_set_color_temp(self._mesh_addr, ct_pct)
            self._attr_color_mode = ColorMode.COLOR_TEMP
        
        # If no specific attributes, just turn on
        if not any(k in kwargs for k in [ATTR_BRIGHTNESS, ATTR_RGB_COLOR, ATTR_COLOR_TEMP_KELVIN]):
            await self.coordinator.async_turn_on(self._mesh_addr)
        
        # Update state
        self.async_write_ha_state()
    
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the light off."""
        _LOGGER.debug("Turn off light %d", self._mesh_addr)
        await self.coordinator.async_turn_off(self._mesh_addr)
        self.async_write_ha_state()
    
    async def async_update(self) -> None:
        """Fetch new state data for this light."""
        await self.coordinator.async_query_status(self._mesh_addr)


class BTAllianceBroadcastLight(BTAllianceMeshLight):
    """Broadcast controls for all lights in a BTAlliance mesh."""

    def __init__(
        self,
        coordinator: BTAllianceMeshCoordinator,
        mesh_name: str,
        entry_id: str,
    ) -> None:
        """Initialize the broadcast light."""
        CoordinatorEntity.__init__(self, coordinator)

        self._mesh_addr = BROADCAST_ADDRESS
        self._mesh_name = mesh_name
        self._entry_id = entry_id
        self._attr_unique_id = f"{entry_id}_all_lights"
        self._attr_name = "All Lights"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=f"{mesh_name} Mesh Hub",
            manufacturer="BTAlliance/Fulife",
            model="Telink BLE Mesh",
        )
        self._broadcast_is_on: bool | None = None
        self._broadcast_brightness: int | None = None
        self._broadcast_color_mode: ColorMode | None = None
        self._broadcast_rgb_color: tuple[int, int, int] | None = None
        self._broadcast_color_temp_kelvin: int | None = None

        coordinator.register_state_callback(BROADCAST_ADDRESS, self._handle_state_update)

    @callback
    def async_removed_from_registry(self) -> None:
        """Handle manual removal from the entity registry."""

    @property
    def is_on(self) -> bool | None:
        """Return the last broadcast on/off state."""
        if self._broadcast_is_on is not None:
            return self._broadcast_is_on

        state = self._sample_light_state()
        if state:
            return state.get('is_on', False)
        return None

    @property
    def brightness(self) -> int | None:
        """Return the last broadcast brightness."""
        if self._broadcast_brightness is not None:
            return self._broadcast_brightness

        state = self._sample_light_state()
        if state and state.get('luminance') is not None:
            return int(state['luminance'] * 255 / 100)
        return None

    @property
    def color_mode(self) -> ColorMode | None:
        """Return the last broadcast color mode."""
        if self._broadcast_color_mode:
            return self._broadcast_color_mode

        state = self._sample_light_state()
        if state and state.get('color_mode') == 'color_temp':
            return ColorMode.COLOR_TEMP
        if state and state.get('color_mode') == 'rgb':
            return ColorMode.RGB
        return self._attr_color_mode

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        """Return the last broadcast RGB color."""
        if self._broadcast_color_mode == ColorMode.RGB:
            return self._broadcast_rgb_color
        if self._broadcast_color_mode == ColorMode.COLOR_TEMP:
            return None

        state = self._sample_light_state()
        if state and state.get('color_mode') != 'color_temp':
            r = state.get('red')
            g = state.get('green')
            b = state.get('blue')
            if r is not None and g is not None and b is not None:
                return (r, g, b)
        return None

    @property
    def color_temp_kelvin(self) -> int | None:
        """Return the last broadcast color temperature."""
        if self._broadcast_color_mode == ColorMode.COLOR_TEMP:
            return self._broadcast_color_temp_kelvin
        if self._broadcast_color_mode == ColorMode.RGB:
            return None

        state = self._sample_light_state()
        if state and state.get('color_temp') is not None:
            ct_pct = state['color_temp']
            return 2700 + int(ct_pct * (6500 - 2700) / 100)
        return None

    def _sample_light_state(self) -> dict[str, Any] | None:
        """Return a representative known light state for the aggregate control."""
        for mesh_addr in sorted(self.coordinator.cached_light_mesh_addresses):
            state = self.coordinator.get_light_state(mesh_addr)
            if state:
                return state
        return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Broadcast light commands to the mesh."""
        _LOGGER.debug("Broadcast turn on with kwargs: %s", kwargs)

        if ATTR_BRIGHTNESS in kwargs:
            self._broadcast_brightness = kwargs[ATTR_BRIGHTNESS]
            await self.coordinator.async_broadcast_set_brightness(self._broadcast_brightness)

        if ATTR_RGB_COLOR in kwargs:
            r, g, b = kwargs[ATTR_RGB_COLOR]
            await self.coordinator.async_broadcast_set_rgb(r, g, b)
            self._broadcast_color_mode = ColorMode.RGB
            self._broadcast_rgb_color = (r, g, b)
            self._broadcast_color_temp_kelvin = None

        elif ATTR_COLOR_TEMP_KELVIN in kwargs:
            kelvin = kwargs[ATTR_COLOR_TEMP_KELVIN]
            ct_pct = int((kelvin - self._attr_min_color_temp_kelvin) * 100 / (
                self._attr_max_color_temp_kelvin - self._attr_min_color_temp_kelvin
            ))
            ct_pct = max(0, min(100, ct_pct))
            await self.coordinator.async_broadcast_set_color_temp(ct_pct)
            self._broadcast_color_mode = ColorMode.COLOR_TEMP
            self._broadcast_color_temp_kelvin = kelvin
            self._broadcast_rgb_color = None

        if not any(k in kwargs for k in [ATTR_BRIGHTNESS, ATTR_RGB_COLOR, ATTR_COLOR_TEMP_KELVIN]):
            await self.coordinator.async_broadcast_turn_on()

        self._broadcast_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Broadcast off to the mesh."""
        _LOGGER.debug("Broadcast turn off")
        await self.coordinator.async_broadcast_turn_off()
        self._broadcast_is_on = False
        self.async_write_ha_state()

    async def async_update(self) -> None:
        """Fetch broadcast mesh status."""
        await self.coordinator.async_broadcast_query_status()
