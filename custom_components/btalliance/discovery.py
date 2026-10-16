"""Network and device identity from compatible Bluetooth advertisements."""

from __future__ import annotations

import re
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from .const import FULIFE_MAC_PREFIXES, SERVICE_UUID


def normalize_mac(value: str) -> str:
    """Validate and normalize a physical device address."""
    compact = value.replace(":", "").replace("-", "").upper()
    if not re.fullmatch(r"[0-9A-F]{12}", compact):
        raise ValueError("Enter a valid Bluetooth MAC address")
    return ":".join(compact[index:index + 2] for index in range(0, 12, 2))


def validate_network_name(value: str) -> str:
    """Keep names compatible with the Alliance app's ten-byte limit."""
    if not value or "\x00" in value or len(value.encode("utf-8")) > 10:
        raise ValueError("Network names must contain 1-10 UTF-8 bytes")
    return value


def validate_password(value: str) -> str:
    """Reject credentials that the protocol would otherwise truncate."""
    if not value or "\x00" in value or len(value.encode("utf-8")) > 16:
        raise ValueError("Passwords must contain 1-16 UTF-8 bytes")
    return value


def parse_addresses(value: str) -> set[int]:
    """Parse exclusions strictly so an invalid address is never silently ignored."""
    result = set()
    for part in re.split(r"[,;\s]+", value.strip()):
        if not part:
            continue
        address = int(part)
        if not 1 <= address <= 254:
            raise ValueError("Mesh addresses must be between 1 and 254")
        result.add(address)
    return result


def parse_macs(value: str) -> set[str]:
    """Parse comma-separated physical-device exclusions."""
    return {normalize_mac(part) for part in re.split(r"[,;\s]+", value.strip()) if part}


def advertisement(info: Any) -> dict[str, Any] | None:
    """Recognize compatible nodes, without treating compatibility as ownership."""
    mac = normalize_mac(info.address)
    manufacturer_data = info.manufacturer_data or {}
    compatible_vendor = any(vendor in (0x1102, 0x0211) for vendor in manufacturer_data)
    known_prefix = any(mac.startswith(prefix) for prefix in FULIFE_MAC_PREFIXES)
    service_match = str(SERVICE_UUID) in [str(uuid).lower() for uuid in info.service_uuids]
    if not (compatible_vendor or known_prefix or service_match or info.name == "Fulife"):
        return None
    name = getattr(getattr(info, "advertisement", None), "local_name", None) or info.name
    if not name or name.upper() == mac or len(name.encode("utf-8")) > 16:
        name = None
    result = {"mac_address": mac, "mesh_name": name, "rssi": info.rssi}
    # HA strips the two manufacturer-ID bytes from manufacturer_data values.
    for vendor, data in manufacturer_data.items():
        if vendor not in (0x1102, 0x0211) or len(data) < 2:
            continue
        result["device_type"] = int.from_bytes(data[0:2], "big")
        if len(data) < 11:
            break
        result["product_id"] = int.from_bytes(data[6:8], "little")
        address = int.from_bytes(data[9:11], "little")
        if 1 <= address <= 254:
            result["mesh_address"] = address
        break
    return result


def is_relay_device(device: dict[str, Any]) -> bool:
    """Identify relays from the vendor's advertised type, never the mesh address."""
    from .device_types import device_type_name

    return device_type_name(device.get('device_type')) == 'Relay'


def discovered_devices(hass: HomeAssistant) -> dict[str, dict[str, Any]]:
    """Return a deduplicated snapshot from HA's Bluetooth scanners."""
    devices = {}
    for info in bluetooth.async_discovered_service_info(hass):
        try:
            device = advertisement(info)
        except ValueError:
            continue
        if device:
            devices[device["mac_address"]] = device
    return devices


def discovered_networks(hass: HomeAssistant) -> list[str]:
    """Return advertised network names, not inferred credentials."""
    return sorted({device["mesh_name"] for device in discovered_devices(hass).values() if device["mesh_name"]})
