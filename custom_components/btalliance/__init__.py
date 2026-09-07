"""BTAlliance Mesh Lights integration for Home Assistant."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr

from .const import (
    DOMAIN,
    CONF_DISCOVERED_LIGHT_MESH_ADDRESSES,
    CONF_GATEWAY_ADDRESS,
    CONF_INFRASTRUCTURE_MESH_ADDRESSES,
    CONF_MESH_NAME,
    CONF_PASSWORD,
)
from .coordinator import BTAllianceMeshCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.LIGHT]


BROADCAST_RGB_SCHEMA = vol.Schema({
    vol.Required("red"): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
    vol.Required("green"): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
    vol.Required("blue"): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
})

BROADCAST_BRIGHTNESS_SCHEMA = vol.Schema({
    vol.Required("brightness"): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
})

BROADCAST_COLOR_TEMP_SCHEMA = vol.Schema({
    vol.Required("color_temp_pct"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
})

CLEANUP_DISCOVERED_LIGHTS_SCHEMA = vol.Schema({
    vol.Optional("entry_id"): str,
    vol.Optional("keep_addresses", default=""): str,
    vol.Optional("require_validation", default=True): bool,
})


def _parse_mesh_addresses(value: Any) -> set[int]:
    """Parse a list of mesh addresses from config data."""
    if value is None:
        return set()

    if isinstance(value, str):
        raw_parts = value.replace(";", ",").replace(" ", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        raw_parts = list(value)
    else:
        raw_parts = [value]

    addresses: set[int] = set()
    for part in raw_parts:
        if part in (None, ""):
            continue
        try:
            address = int(str(part).strip())
        except ValueError:
            _LOGGER.warning("Ignoring invalid BTAlliance mesh address exclusion: %s", part)
            continue
        if 1 <= address <= 254:
            addresses.add(address)
        else:
            _LOGGER.warning("Ignoring out-of-range BTAlliance mesh address: %s", part)
    return addresses


async def _get_coordinator(
    hass: HomeAssistant,
    call: ServiceCall,
) -> BTAllianceMeshCoordinator:
    """Return the coordinator to use for a service call."""
    coordinators = hass.data.get(DOMAIN, {})

    if len(coordinators) == 1:
        return next(iter(coordinators.values()))

    entry_id = call.data.get("entry_id")
    if entry_id and entry_id in coordinators:
        return coordinators[entry_id]

    raise ValueError(
        "Multiple BTAlliance hubs are configured; provide entry_id."
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up BTAlliance Mesh Lights from a config entry."""
    _LOGGER.debug("Setting up BTAlliance entry: %s", entry.entry_id)

    gateway_address = entry.data[CONF_GATEWAY_ADDRESS]
    mesh_name = entry.options.get(CONF_MESH_NAME, entry.data[CONF_MESH_NAME])
    password = entry.options.get(CONF_PASSWORD, entry.data[CONF_PASSWORD])
    infrastructure_mesh_addresses = _parse_mesh_addresses(
        entry.options.get(
            CONF_INFRASTRUCTURE_MESH_ADDRESSES,
            entry.data.get(CONF_INFRASTRUCTURE_MESH_ADDRESSES, ""),
        )
    )
    cached_light_mesh_addresses = _parse_mesh_addresses(
        entry.options.get(
            CONF_DISCOVERED_LIGHT_MESH_ADDRESSES,
            entry.data.get(CONF_DISCOVERED_LIGHT_MESH_ADDRESSES, ""),
        )
    )

    coordinator = BTAllianceMeshCoordinator(
        hass=hass,
        entry=entry,
        gateway_address=gateway_address,
        mesh_name=mesh_name,
        password=password,
        infrastructure_mesh_addresses=infrastructure_mesh_addresses,
        cached_light_mesh_addresses=cached_light_mesh_addresses,
    )

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name=f"{mesh_name} Mesh Hub",
        manufacturer="BTAlliance/Fulife",
        model="Telink BLE Mesh Gateway",
    )

    if (
        not hass.services.has_service(DOMAIN, "broadcast_turn_on")
        or not hass.services.has_service(DOMAIN, "cleanup_discovered_lights")
    ):

        async def handle_broadcast_turn_on(call: ServiceCall) -> None:
            coordinator = await _get_coordinator(hass, call)
            await coordinator.async_broadcast_turn_on()

        async def handle_broadcast_turn_off(call: ServiceCall) -> None:
            coordinator = await _get_coordinator(hass, call)
            await coordinator.async_broadcast_turn_off()

        async def handle_broadcast_query_status(call: ServiceCall) -> None:
            coordinator = await _get_coordinator(hass, call)
            await coordinator.async_broadcast_query_status()

        async def handle_broadcast_sync_time(call: ServiceCall) -> None:
            coordinator = await _get_coordinator(hass, call)
            await coordinator.async_broadcast_sync_time()

        async def handle_broadcast_set_brightness(call: ServiceCall) -> None:
            coordinator = await _get_coordinator(hass, call)
            await coordinator.async_broadcast_set_brightness(
                call.data["brightness"]
            )

        async def handle_broadcast_set_rgb(call: ServiceCall) -> None:
            coordinator = await _get_coordinator(hass, call)
            await coordinator.async_broadcast_set_rgb(
                call.data["red"],
                call.data["green"],
                call.data["blue"],
            )

        async def handle_broadcast_set_color_temp(call: ServiceCall) -> None:
            coordinator = await _get_coordinator(hass, call)
            await coordinator.async_broadcast_set_color_temp(
                call.data["color_temp_pct"]
            )

        async def handle_cleanup_discovered_lights(call: ServiceCall) -> None:
            coordinator = await _get_coordinator(hass, call)
            keep_addresses = _parse_mesh_addresses(call.data.get("keep_addresses", ""))
            removed = coordinator.cleanup_cached_light_addresses(
                keep_addresses=keep_addresses,
                require_validation=call.data["require_validation"],
            )
            _LOGGER.info(
                "BTAlliance cleanup_discovered_lights removed %d addresses",
                len(removed),
            )

        if not hass.services.has_service(DOMAIN, "broadcast_turn_on"):
            hass.services.async_register(
                DOMAIN,
                "broadcast_turn_on",
                handle_broadcast_turn_on,
            )

        if not hass.services.has_service(DOMAIN, "broadcast_turn_off"):
            hass.services.async_register(
                DOMAIN,
                "broadcast_turn_off",
                handle_broadcast_turn_off,
            )

        if not hass.services.has_service(DOMAIN, "broadcast_query_status"):
            hass.services.async_register(
                DOMAIN,
                "broadcast_query_status",
                handle_broadcast_query_status,
            )

        if not hass.services.has_service(DOMAIN, "broadcast_sync_time"):
            hass.services.async_register(
                DOMAIN,
                "broadcast_sync_time",
                handle_broadcast_sync_time,
            )

        if not hass.services.has_service(DOMAIN, "broadcast_set_brightness"):
            hass.services.async_register(
                DOMAIN,
                "broadcast_set_brightness",
                handle_broadcast_set_brightness,
                schema=BROADCAST_BRIGHTNESS_SCHEMA,
            )

        if not hass.services.has_service(DOMAIN, "broadcast_set_rgb"):
            hass.services.async_register(
                DOMAIN,
                "broadcast_set_rgb",
                handle_broadcast_set_rgb,
                schema=BROADCAST_RGB_SCHEMA,
            )

        if not hass.services.has_service(DOMAIN, "broadcast_set_color_temp"):
            hass.services.async_register(
                DOMAIN,
                "broadcast_set_color_temp",
                handle_broadcast_set_color_temp,
                schema=BROADCAST_COLOR_TEMP_SCHEMA,
            )

        if not hass.services.has_service(DOMAIN, "cleanup_discovered_lights"):
            hass.services.async_register(
                DOMAIN,
                "cleanup_discovered_lights",
                handle_cleanup_discovered_lights,
                schema=CLEANUP_DISCOVERED_LIGHTS_SCHEMA,
            )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.debug("Unloading BTAlliance entry: %s", entry.entry_id)

    unload_ok = await hass.config_entries.async_unload_platforms(
        entry,
        PLATFORMS,
    )

    if unload_ok:
        coordinator: BTAllianceMeshCoordinator = hass.data[DOMAIN].pop(
            entry.entry_id
        )
        await coordinator.async_disconnect()

    return unload_ok


async def async_reload_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
