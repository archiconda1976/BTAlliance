"""Direct, selected-MAC provisioning using the app's pairing operations."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import START_SESSION_UUID, FIRMWARE_UUID, DEFAULT_MESH_NAME, DEFAULT_PASSWORD
from .discovery import normalize_mac, validate_network_name, validate_password
from .protocol import TelinkProtocol


async def manage_device(
    hass: HomeAssistant,
    mac: str,
    current_name: str,
    current_password: str,
    operation: str,
    new_name: str | None = None,
    new_password: str = DEFAULT_PASSWORD,
) -> dict[str, Any]:
    """Operate on one physical device, with no fallback gateway or retries."""
    mac = normalize_mac(mac)
    if operation not in ("move", "reset", "inspect"):
        raise ValueError("Unsupported device operation")
    if operation == "move":
        validate_network_name(new_name or "")
        validate_password(new_password)
    device = async_ble_device_from_address(hass, mac, connectable=True)
    if device is None:
        raise HomeAssistantError("The selected Bluetooth device is not reachable")
    client = None
    result: dict[str, Any] = {"mac_address": mac, "operation": operation}
    try:
        async with asyncio.timeout(45):
            client = await establish_connection(BleakClientWithServiceCache, device, mac)
            protocol = TelinkProtocol(int(mac.replace(":", ""), 16).to_bytes(6, "little"), current_name, current_password)
            pair_uuid = str(START_SESSION_UUID)
            random = os.urandom(8)
            await client.write_gatt_char(pair_uuid, bytes([0x0C]) + bytes(protocol.generate_login_payload(random)), response=True)
            await asyncio.sleep(0.1)
            response = await client.read_gatt_char(pair_uuid)
            if not protocol.process_login_response(response, random):
                raise HomeAssistantError("Authentication to the selected device failed")
            if operation == "inspect":
                try:
                    revision = await client.read_gatt_char(FIRMWARE_UUID)
                    result["firmware_version"] = bytes(revision).rstrip(b"\x00").decode("utf-8", errors="replace")
                except Exception as err:
                    raise HomeAssistantError("This device did not provide its GATT firmware revision") from err
                result["status"] = "metadata_read"
                return result
            if operation == "move":
                for packet in protocol.generate_network_packets(new_name, new_password):
                    await client.write_gatt_char(pair_uuid, packet, response=True)
                response = await client.read_gatt_char(pair_uuid)
                if not protocol.validate_network_confirmation(response, new_name, new_password):
                    raise HomeAssistantError("Network change was not confirmed; inspect the device before retrying")
                result["mesh_name"] = new_name
            else:
                await client.write_gatt_char(pair_uuid, protocol.generate_factory_reset_packet(os.urandom(8)), response=True)
                response = await client.read_gatt_char(pair_uuid)
                if not response or response[0] != 0x0B:
                    raise HomeAssistantError("Factory reset was not confirmed; inspect the device before retrying")
                result["mesh_name"] = DEFAULT_MESH_NAME
            result["status"] = "pairing_confirmed"
            result["reconnect_verified"] = False
            return result
    except HomeAssistantError:
        raise
    except Exception as err:
        # A write may have reached the device even when a later read times out.
        raise HomeAssistantError("Device operation failed or its outcome is uncertain; inspect before retrying") from err
    finally:
        if client is not None:
            try:
                async with asyncio.timeout(5):
                    await client.disconnect()
            except Exception:
                pass
