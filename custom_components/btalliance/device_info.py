"""Keep physical device registry details aligned with mesh identity metadata."""

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo


def apply_device_metadata(info: DeviceInfo, metadata: dict, fallback_model: str) -> None:
    """Expose only an unambiguous model and physical Bluetooth connection."""
    info['model'] = metadata.get('device_type_name') or fallback_model
    mac = metadata.get('mac_address') if not metadata.get('address_collision') else None
    info['connections'] = {(dr.CONNECTION_BLUETOOTH, mac)} if mac else set()


@callback
def sync_device_info(hass: HomeAssistant, entry_id: str, info: DeviceInfo) -> None:
    """Update an existing device without changing its mesh-based identity."""
    registry = dr.async_get(hass)
    device = registry.async_get_device(identifiers=info['identifiers'])
    if device is None:
        return
    connections = {connection for connection in device.connections
                   if connection[0] != dr.CONNECTION_BLUETOOTH} | info.get('connections', set())
    if connections != device.connections:
        registry.async_update_device(device.id, new_connections=connections)
    if device.model != info.get('model'):
        registry.async_get_or_create(config_entry_id=entry_id, **info)
