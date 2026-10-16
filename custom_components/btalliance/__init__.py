"""BTAlliance Mesh Lights integration for Home Assistant."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import (
    DOMAIN,
    CONF_DISCOVERED_LIGHT_MESH_ADDRESSES,
    CONF_GATEWAY_ADDRESS,
    CONF_INFRASTRUCTURE_MESH_ADDRESSES,
    CONF_MESH_NAME,
    CONF_PASSWORD,
    CONF_EXCLUDED_MESH_ADDRESSES,
    CONF_EXCLUDED_MAC_ADDRESSES,
    CONF_DISCOVERY_MODE, DISCOVERY_PASSIVE,
)
from .passive import PassiveNetworkMonitor
from .coordinator import BTAllianceMeshCoordinator
from .groups import validate_group_address
from .share_settings import IMPORT_KEY, IMPORT_ENROLLMENT_VERSION, restore_pending_import
from .discovery import parse_addresses, parse_macs, normalize_mac, validate_network_name, validate_password

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.LIGHT]


BROADCAST_RGB_SCHEMA = vol.Schema({
    vol.Optional("entry_id"): str,
    vol.Required("red"): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
    vol.Required("green"): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
    vol.Required("blue"): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
})

BROADCAST_BRIGHTNESS_SCHEMA = vol.Schema({
    vol.Optional("entry_id"): str,
    vol.Required("brightness"): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
})

BROADCAST_COLOR_TEMP_SCHEMA = vol.Schema({
    vol.Optional("entry_id"): str,
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
    entry_id = call.data.get("entry_id")
    if entry_id in coordinators and isinstance(coordinators[entry_id], PassiveNetworkMonitor):
        raise HomeAssistantError('Passive monitors cannot send device commands')
    if entry_id and entry_id in coordinators:
        return coordinators[entry_id]
    if entry_id:
        raise HomeAssistantError("Unknown BTAlliance hub entry_id")
    active = [hub for hub in coordinators.values() if not isinstance(hub, PassiveNetworkMonitor)]
    if len(active) == 1:
        return active[0]
    if not active:
        raise HomeAssistantError('No active BTAlliance hub is configured')

    raise ValueError(
        "Multiple BTAlliance hubs are configured; provide entry_id."
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up BTAlliance Mesh Lights from a config entry."""
    _LOGGER.debug("Setting up BTAlliance entry: %s", entry.entry_id)

    if entry.options.get(CONF_DISCOVERY_MODE, entry.data.get(CONF_DISCOVERY_MODE)) == DISCOVERY_PASSIVE:
        monitor = PassiveNetworkMonitor(hass, entry)
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = monitor
        await monitor.async_start()
        await hass.config_entries.async_forward_entry_setups(entry, [Platform.BINARY_SENSOR])
        async def reload_passive_options(hass, updated):
            if updated.options.get(CONF_MESH_NAME, updated.data[CONF_MESH_NAME]) != monitor.mesh_name:
                await hass.config_entries.async_reload(updated.entry_id)
        entry.async_on_unload(entry.add_update_listener(reload_passive_options))
        return True

    # Discard the obsolete manual/app-export type cache, preserving learned identities.
    if 'cached_device_types' in entry.options:
        options = dict(entry.options)
        options.pop('cached_device_types')
        hass.config_entries.async_update_entry(entry, options=options)

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

    if entry.options.get(IMPORT_KEY) and entry.options.get(IMPORT_ENROLLMENT_VERSION) != 1:
        # Preserve persisted enrollment; old import metadata is not a new grant.
        options = dict(entry.options)
        options[IMPORT_ENROLLMENT_VERSION] = 1
        if entry.options[IMPORT_KEY].get('mesh_name') == mesh_name:
            prefix = f'{entry.entry_id}_infrastructure_'
            for registered in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id):
                if registered.domain != 'binary_sensor' or not registered.unique_id.startswith(prefix):
                    continue
                try:
                    address = int(registered.unique_id.removeprefix(prefix))
                except ValueError:
                    continue
                if 1 <= address <= 254:
                    infrastructure_mesh_addresses.add(address)
            options[CONF_INFRASTRUCTURE_MESH_ADDRESSES] = ','.join(map(str, sorted(infrastructure_mesh_addresses)))
        hass.config_entries.async_update_entry(entry, options=options)

    coordinator = BTAllianceMeshCoordinator(
        hass=hass,
        entry=entry,
        gateway_address=gateway_address,
        mesh_name=mesh_name,
        password=password,
        infrastructure_mesh_addresses=infrastructure_mesh_addresses,
        cached_light_mesh_addresses=cached_light_mesh_addresses,
        excluded_mesh_addresses=parse_addresses(entry.options.get(CONF_EXCLUDED_MESH_ADDRESSES, entry.data.get(CONF_EXCLUDED_MESH_ADDRESSES, ""))),
        excluded_mac_addresses=parse_macs(entry.options.get(CONF_EXCLUDED_MAC_ADDRESSES, entry.data.get(CONF_EXCLUDED_MAC_ADDRESSES, ""))),
    )

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator
    restore_pending_import(hass, entry, mesh_name)

    settings_keys = (CONF_MESH_NAME, CONF_PASSWORD, CONF_INFRASTRUCTURE_MESH_ADDRESSES,
                     CONF_EXCLUDED_MESH_ADDRESSES, CONF_EXCLUDED_MAC_ADDRESSES, "enrolled_mesh_addresses", "discovery_mode", IMPORT_KEY)
    settings = {key: entry.options.get(key, entry.data.get(key, "")) for key in settings_keys}

    async def update_options(hass: HomeAssistant, updated: ConfigEntry) -> None:
        effective = {key: updated.options.get(key, updated.data.get(key, "")) for key in settings_keys}
        if effective != settings:
            await hass.config_entries.async_reload(updated.entry_id)

    entry.async_on_unload(entry.add_update_listener(update_options))

    async def handle_device_operation(call: ServiceCall) -> dict[str, Any]:
        hub = await _get_coordinator(hass, call)
        operation = {"move_device_to_network": "move", "reset_device_to_factory": "reset", "refresh_device_metadata": "inspect"}[call.service]
        if operation != "inspect" and not call.data["confirm"]:
            raise HomeAssistantError("Explicit confirmation is required for a device network change")
        return await hub.async_manage_device(call.data["mac_address"], operation,
                                             call.data.get("new_mesh_name"), call.data.get("new_password", "2846"))

    async def handle_query_firmware(call: ServiceCall) -> None:
        hub = await _get_coordinator(hass, call)
        if not await hub.async_query_firmware(call.data["mesh_address"]):
            raise HomeAssistantError("Firmware query could not be sent")

    async def handle_group(call: ServiceCall) -> dict[str, Any]:
        hub = await _get_coordinator(hass, call)
        if call.service == 'configure_group':
            hub.configure_group(call.data['group_address'], call.data['name'])
            return hub.get_group_metadata(call.data['group_address'])
        address = call.data['mesh_address']
        if call.service == 'query_light_groups':
            groups = await hub.async_query_groups(address)
            return {'mesh_address': address, 'group_addresses': sorted(groups) if groups is not None else None,
                    'confirmed': groups is not None}
        if not call.data['confirm']:
            raise HomeAssistantError('Confirm the membership change; duplicate mesh addresses can affect other devices')
        await hub.async_set_group_membership(address, call.data['group_address'], call.service == 'add_light_to_group')
        return dict(hub.last_group_operation)

    group_schema = {vol.Optional('entry_id'): str,
                    vol.Required('group_address'): validate_group_address,
                    vol.Required('mesh_address'): vol.All(vol.Coerce(int), vol.Range(min=1, max=254)),
                    vol.Required('confirm'): bool}
    for service in ('add_light_to_group', 'remove_light_from_group'):
        if not hass.services.has_service(DOMAIN, service):
            hass.services.async_register(DOMAIN, service, handle_group, schema=vol.Schema(group_schema),
                                         supports_response=SupportsResponse.OPTIONAL)
    for service, schema in (
        ('configure_group', {vol.Optional('entry_id'): str, vol.Required('group_address'): validate_group_address,
                             vol.Required('name'): vol.All(str, vol.Length(min=1, max=80))}),
        ('query_light_groups', {vol.Optional('entry_id'): str,
                               vol.Required('mesh_address'): vol.All(vol.Coerce(int), vol.Range(min=1, max=254))}),
    ):
        if not hass.services.has_service(DOMAIN, service):
            hass.services.async_register(DOMAIN, service, handle_group, schema=vol.Schema(schema),
                                         supports_response=SupportsResponse.OPTIONAL)

    device_schema = {vol.Optional("entry_id"): str, vol.Required("mac_address"): normalize_mac}
    for name in ("move_device_to_network", "reset_device_to_factory", "refresh_device_metadata"):
        schema = dict(device_schema)
        if name != "refresh_device_metadata":
            schema[vol.Required("confirm")] = bool
        if name == "move_device_to_network":
            schema[vol.Required("new_mesh_name")] = validate_network_name
            schema[vol.Optional("new_password", default="2846")] = validate_password
        if not hass.services.has_service(DOMAIN, name):
            hass.services.async_register(DOMAIN, name, handle_device_operation,
                                         schema=vol.Schema(schema), supports_response=SupportsResponse.OPTIONAL)
    if not hass.services.has_service(DOMAIN, "query_device_firmware"):
        hass.services.async_register(DOMAIN, "query_device_firmware", handle_query_firmware, schema=vol.Schema({
            vol.Optional("entry_id"): str,
            vol.Required("mesh_address"): vol.All(vol.Coerce(int), vol.Range(min=1, max=254)),
        }))

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

    passive = entry.data.get(CONF_DISCOVERY_MODE) == DISCOVERY_PASSIVE
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry,
        [Platform.BINARY_SENSOR] if passive else PLATFORMS,
    )

    if unload_ok:
        coordinator: BTAllianceMeshCoordinator = hass.data[DOMAIN].pop(
            entry.entry_id
        )
        if isinstance(coordinator, PassiveNetworkMonitor):
            await coordinator.async_stop()
        else:
            await coordinator.async_disconnect()

    return unload_ok


async def async_reload_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
