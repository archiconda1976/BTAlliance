"""Coordinator for BTAlliance mesh device management."""

import asyncio
import contextlib
import logging
import os
import time
from copy import deepcopy
from typing import Any, Callable, Dict, Optional

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from bleak_retry_connector import establish_connection, BleakClientWithServiceCache
from bleak import BleakClient
from bleak.exc import BleakError

from datetime import timedelta

from .const import (
    DOMAIN,
    CONF_DISCOVERED_LIGHT_MESH_ADDRESSES,
    CONF_INFRASTRUCTURE_MESH_ADDRESSES,
    SERVICE_UUID, START_SESSION_UUID, NOTIFY_UUID, COMMAND_UUID,
    NOTIFY_STATUS_RESPONSE, NOTIFY_LIGHT_STATUS,
    MAX_CONNECTION_RETRIES, CONNECTION_TIMEOUT, LOGIN_TIMEOUT,
    DISCONNECT_TIMEOUT, RETRY_DELAY, MESH_DISCOVERY_TIMEOUT,
    BROADCAST_ADDRESS, POLLING_INTERVAL, FULIFE_MAC_PREFIXES,
)
from .protocol import TelinkProtocol
from .const import CONF_EXCLUDED_MESH_ADDRESSES, CONF_EXCLUDED_MAC_ADDRESSES, CONF_DEVICE_METADATA, NOTIFY_FIRMWARE, CONF_ENROLLED_MESH_ADDRESSES
from .discovery import discovered_devices, discovered_networks, advertisement, normalize_mac, parse_addresses, is_relay_device
from .const import CONF_DISCOVERY_MODE, DISCOVERY_AUTOMATIC, DISCOVERY_APPROVAL
from .const import CONF_MESH_GROUPS, NOTIFY_GROUPS
from .groups import validate_group_address, aggregate_group_state
from .device_types import device_type_name
from .network_management import manage_device
from .share_settings import IMPORT_KEY

_LOGGER = logging.getLogger(__name__)

# UUID strings for bleak
SERVICE_UUID_STR = "00010203-0405-0607-0809-0a0b0c0d1910"
START_SESSION_UUID_STR = "00010203-0405-0607-0809-0a0b0c0d1914"
NOTIFY_UUID_STR = "00010203-0405-0607-0809-0a0b0c0d1911"
COMMAND_UUID_STR = "00010203-0405-0607-0809-0a0b0c0d1912"
CONNECT_ATTEMPT_TIMEOUT = 8.0
MAX_FALLBACK_GATEWAY_CANDIDATES = 4
MESH_LIGHT_CONFIRMATION_COUNT = 2
MAX_DISCOVERY_VALIDATION_TASKS = 3
DISCOVERY_VALIDATION_COOLDOWN = 120.0
CONFIGURED_GATEWAY_FAILURE_BACKOFF = 15.0
FALLBACK_GATEWAY_CONNECT_BACKOFF = 120.0
FALLBACK_GATEWAY_SESSION_BACKOFF = 900.0
COMMAND_BURST_COUNT = 5
COMMAND_BURST_DELAY = 0.35
COMMAND_WRITE_WITH_RESPONSE = True
NETWORK_CHANGE_ADVERTISEMENT_GRACE = 30.0


def _mac_to_int(mac: str) -> int:
    """Convert a MAC address string to an integer."""
    return int(mac.replace(":", "").replace("-", ""), 16)


def _is_fulife_service_info(service_info: BluetoothServiceInfoBleak) -> bool:
    """Return True if a Bluetooth discovery looks like a Fulife mesh node."""
    if service_info.name and service_info.name.startswith("Fulife"):
        return True

    mac = service_info.address.upper()
    return any(mac.startswith(prefix) for prefix in FULIFE_MAC_PREFIXES)


class BTAllianceMeshCoordinator(DataUpdateCoordinator):
    """Coordinator for managing BTAlliance mesh network."""
    
    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        gateway_address: int,
        mesh_name: str,
        password: str,
        infrastructure_mesh_addresses: set[int] | None = None,
        cached_light_mesh_addresses: set[int] | None = None,
        excluded_mesh_addresses: set[int] | None = None,
        excluded_mac_addresses: set[str] | None = None,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=POLLING_INTERVAL),
        )
        
        self.entry = entry
        self.gateway_address = gateway_address
        self.mesh_name = mesh_name
        self.password = password
        self.excluded_mesh_addresses = excluded_mesh_addresses or set()
        self.excluded_mac_addresses = excluded_mac_addresses or set()
        self.device_metadata = deepcopy(dict(entry.options.get(CONF_DEVICE_METADATA, {})))
        firmware_cache = entry.options.get("firmware_cache", {})
        self.firmware_names: dict[int, str] = {
            int(address): name for address, name in firmware_cache.get("names", {}).items()
        } if firmware_cache.get("mesh_name") == mesh_name else {}
        self._firmware_requested: set[int] = set()
        self._last_query_sent = 0.0
        group_cache = entry.options.get(CONF_MESH_GROUPS, {})
        self.mesh_groups: dict[int, dict[str, Any]] = {}
        self.light_groups: dict[int, set[int]] = {}
        self.group_membership_verified: dict[int, float] = {}
        if group_cache.get('mesh_name') == mesh_name:
            for address, group in group_cache.get('groups', {}).items():
                try:
                    self.mesh_groups[validate_group_address(address)] = {'name': str(group['name'])}
                except (ValueError, KeyError, TypeError):
                    continue
            for address, membership in group_cache.get('memberships', {}).items():
                try:
                    node = int(address)
                    if 1 <= node <= 254:
                        self.light_groups[node] = {validate_group_address(group) for group in membership['groups']}
                        self.group_membership_verified[node] = float(membership.get('verified_at', 0))
                except (ValueError, KeyError, TypeError):
                    continue
        self._group_reply_pending: dict[int, asyncio.Future] = {}
        self._group_membership_lock = asyncio.Lock()
        self._new_group_callback: Callable[[int], None] | None = None
        self.last_group_operation: dict[str, Any] | None = None
        self._management_active = False
        self.last_device_operation: dict[str, Any] | None = None
        self.infrastructure_mesh_addresses = infrastructure_mesh_addresses or set()
        self.cached_light_mesh_addresses = cached_light_mesh_addresses or set()
        self.discovery_mode = entry.options.get(CONF_DISCOVERY_MODE, entry.data.get(CONF_DISCOVERY_MODE, DISCOVERY_AUTOMATIC))
        enrolled = entry.options.get(CONF_ENROLLED_MESH_ADDRESSES, entry.data.get(CONF_ENROLLED_MESH_ADDRESSES))
        self.enrolled_mesh_addresses = parse_addresses(enrolled) if enrolled is not None else set(self.cached_light_mesh_addresses)
        self.observed_mesh_addresses: dict[int, dict[str, Any]] = {}
        if self.discovery_mode == DISCOVERY_APPROVAL:
            self.cached_light_mesh_addresses.intersection_update(self.enrolled_mesh_addresses)
        else:
            self.enrolled_mesh_addresses.update(self.cached_light_mesh_addresses)
        self.cached_light_mesh_addresses.update(self.enrolled_mesh_addresses)
        self.cached_light_mesh_addresses.difference_update(self.infrastructure_mesh_addresses)
        self.cached_light_mesh_addresses.difference_update(self.excluded_mesh_addresses)
        imported = entry.options.get(IMPORT_KEY, {})
        self.imported_devices = imported.get('devices', {}) if imported.get('mesh_name') == mesh_name else {}
        for raw, node in self.imported_devices.items():
            address = int(raw)
            if self.is_excluded_address(address) or node.get('mac_address') in self.excluded_mac_addresses:
                self.cached_light_mesh_addresses.discard(address)
                self.enrolled_mesh_addresses.discard(address)
                continue
        if imported.get('mesh_name') == mesh_name:
            for raw in self.imported_devices:
                member = int(raw)
                if member in self.enrolled_mesh_addresses and not self.group_membership_verified.get(member):
                    self.light_groups[member] = {int(key) for key, value in imported.get('groups', {}).items()
                                                 if member in value['members']}
            for raw, group in imported.get('groups', {}).items():
                address = validate_group_address(raw)
                self.mesh_groups.setdefault(address, {'name': group['name']})
        self.mac_bytes = gateway_address.to_bytes(6, "little")
        
        # Protocol handler
        self.protocol = TelinkProtocol(self.mac_bytes, mesh_name, password)
        self._command_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        
        # Connection state
        self.connected = False
        self.login_valid = False
        self.ble_device = None
        self.client = None
        self.selected_gateway_address: str | None = None
        self.selected_gateway_name: str | None = None
        self.selected_gateway_rssi: int | None = None
        self.gateway_candidates: Dict[str, Dict[str, Any]] = {}
        self.gateway_candidate_failures: Dict[str, Dict[str, Any]] = {}
        self.start_session_handle: Optional[int] = None
        self.notify_handle: Optional[int] = None
        self.command_handle: Optional[int] = None
        
        # Mesh devices discovered via 0xDC notifications
        self.discovered_devices: Dict[int, Dict[str, Any]] = {}
        self.pending_discovered_devices: Dict[int, Dict[str, Any]] = {}
        self.validated_light_mesh_addresses: set[int] = set()
        self._discovery_validation_tasks: set[asyncio.Task] = set()
        
        # Light state cache per mesh address
        self.light_states: Dict[int, Dict[str, Any]] = {}

        # Infrastructure node state cache per mesh address
        self.infrastructure_states: Dict[int, Dict[str, Any]] = {
            address: {'last_seen': None} for address in self.infrastructure_mesh_addresses
        }
        
        # Callbacks for state updates
        self._state_callbacks: Dict[int, list[Callable]] = {}
        
        # Discovery event
        self._discovery_complete = asyncio.Event()
        
        # Callback for adding new entities dynamically
        self._new_device_callback: Optional[Callable[[int], None]] = None
        self._new_infrastructure_callback: Optional[Callable[[int], None]] = None

        if self.infrastructure_mesh_addresses:
            _LOGGER.info(
                "BTAlliance mesh infrastructure addresses: %s",
                sorted(self.infrastructure_mesh_addresses),
            )
        if self.cached_light_mesh_addresses:
            _LOGGER.info(
                "BTAlliance cached mesh light addresses: %s",
                sorted(self.cached_light_mesh_addresses),
            )
        
        
    
    def set_new_device_callback(self, callback: Callable[[int], None]) -> None:
        """Set callback to be called when new mesh devices are discovered."""
        self._new_device_callback = callback

    def set_new_infrastructure_callback(self, callback: Callable[[int], None]) -> None:
        """Set callback to be called when new infrastructure nodes are discovered."""
        self._new_infrastructure_callback = callback

    def _persist_cached_light_addresses(self, *, persist_enrollment: bool = False) -> None:
        """Persist known light mesh addresses to the config entry options."""
        value = ",".join(str(address) for address in sorted(self.cached_light_mesh_addresses))
        options = dict(self.entry.options)
        options[CONF_DISCOVERED_LIGHT_MESH_ADDRESSES] = value
        if persist_enrollment:
            options[CONF_ENROLLED_MESH_ADDRESSES] = ','.join(map(str, sorted(self.enrolled_mesh_addresses)))
        self.hass.config_entries.async_update_entry(self.entry, options=options)

    def _persist_infrastructure_addresses(self) -> None:
        """Persist known infrastructure mesh addresses to the config entry options."""
        value = ",".join(str(address) for address in sorted(self.infrastructure_mesh_addresses))
        options = dict(self.entry.options)
        options[CONF_INFRASTRUCTURE_MESH_ADDRESSES] = value
        self.hass.config_entries.async_update_entry(self.entry, options=options)

    def add_infrastructure_address(self, mesh_addr: int) -> None:
        """Remember a mesh infrastructure address across restarts."""
        if not 1 <= mesh_addr <= 254:
            return

        is_new = mesh_addr not in self.infrastructure_mesh_addresses
        self.infrastructure_mesh_addresses.add(mesh_addr)
        self.infrastructure_states.setdefault(mesh_addr, {'last_seen': None})
        self.remove_cached_light_address(mesh_addr)
        self._remove_infrastructure_light_entity(mesh_addr)

        if is_new:
            self._persist_infrastructure_addresses()
            _LOGGER.info("Cached BTAlliance mesh infrastructure address %d", mesh_addr)

            if self._new_infrastructure_callback:
                self._new_infrastructure_callback(mesh_addr)

    def _remove_infrastructure_light_entity(self, mesh_addr: int) -> None:
        """Remove a light entity registry entry for an address now known as infrastructure."""
        entity_registry = er.async_get(self.hass)
        entity_id = entity_registry.async_get_entity_id(
            "light",
            DOMAIN,
            f"{self.entry.entry_id}_{mesh_addr}",
        )
        if entity_id:
            _LOGGER.info(
                "Removing BTAlliance light entity for infrastructure address %d: %s",
                mesh_addr,
                entity_id,
            )
            entity_registry.async_remove(entity_id)

    def add_cached_light_address(self, mesh_addr: int) -> None:
        """Remember a light mesh address across restarts."""
        if not self._discovery_allows(mesh_addr) or not self._is_controllable_light_address(mesh_addr):
            return
        self.enrolled_mesh_addresses.add(mesh_addr)
        if mesh_addr in self.cached_light_mesh_addresses:
            return

        self.cached_light_mesh_addresses.add(mesh_addr)
        self._persist_cached_light_addresses()
        _LOGGER.info("Cached BTAlliance mesh light address %d", mesh_addr)

    def _confirm_light_address(self, mesh_addr: int, state: Dict[str, Any], reason: str) -> None:
        """Confirm a mesh address as a controllable light."""
        if not self._discovery_allows(mesh_addr) or not self._is_controllable_light_address(mesh_addr):
            return

        was_cached = mesh_addr in self.cached_light_mesh_addresses
        is_new_device = mesh_addr not in self.discovered_devices and not was_cached
        self.pending_discovered_devices.pop(mesh_addr, None)
        self.validated_light_mesh_addresses.add(mesh_addr)
        self.add_cached_light_address(mesh_addr)
        self.discovered_devices[mesh_addr] = state
        self.light_states[mesh_addr] = state

        _LOGGER.info("Confirmed mesh light %d from %s", mesh_addr, reason)
        if is_new_device and self._new_device_callback:
            _LOGGER.info("New mesh device discovered: %d", mesh_addr)
            self._new_device_callback(mesh_addr)

        self._notify_state_change(mesh_addr)

    def apply_enrollment(self, addresses: set[int]) -> None:
        """Only explicitly selected addresses may become light entities."""
        self.enrolled_mesh_addresses = set(addresses)
        for address in list(self.cached_light_mesh_addresses - addresses):
            self.remove_cached_light_address(address)
            self._remove_light_entity_and_device(address)
        for address in sorted(addresses):
            self.add_cached_light_address(address)

    def _discovery_allows(self, address: int) -> bool:
        """Automatic discovery does not require a pre-existing address allowlist."""
        return self.discovery_mode == DISCOVERY_AUTOMATIC or address in self.enrolled_mesh_addresses

    def refresh_advertisement_metadata(self) -> dict[str, dict[str, Any]]:
        """Remember only identity metadata, never inferred ownership or passwords."""
        devices = discovered_devices(self.hass)
        changed = False
        changed_addresses: set[int] = set()
        for mac, device in devices.items():
            if device.get("mesh_name") != self.mesh_name:
                continue
            saved = self.device_metadata.get(mac, {})
            if (saved.get("pending_network_change") and saved.get("mesh_name") != device["mesh_name"]
                    and time.time() < saved.get("pending_network_change_until", 0)):
                continue
            # RSSI is live and must not cause configuration writes every poll.
            identity = {key: value for key, value in device.items() if key != "rssi"}
            merged = {**saved, **identity}
            merged.pop("pending_network_change", None)
            merged.pop("pending_network_change_until", None)
            if merged != saved:
                self.device_metadata[mac] = merged
                changed = True
                for address in (saved.get("mesh_address"), merged.get("mesh_address")):
                    if isinstance(address, int) and 1 <= address <= 254:
                        changed_addresses.add(address)
        if changed:
            self._persist_device_metadata()
        for node in self.device_metadata.values():
            address = node.get('mesh_address')
            if (node.get('mesh_name') == self.mesh_name and address
                    and address not in self.infrastructure_mesh_addresses and is_relay_device(node)):
                self.add_infrastructure_address(address)
        if changed:
            for address in sorted(changed_addresses):
                self._notify_state_change(address)
        return devices

    def _persist_device_metadata(self) -> None:
        options = dict(self.entry.options)
        options[CONF_DEVICE_METADATA] = deepcopy(self.device_metadata)
        options["firmware_cache"] = {"mesh_name": self.mesh_name, "names": {str(address): name for address, name in self.firmware_names.items()}}
        self.hass.config_entries.async_update_entry(self.entry, options=options)

    def get_device_metadata(self, mesh_addr: int) -> dict[str, Any]:
        """Expose identity collisions instead of guessing a physical device."""
        nodes = [data for data in self.device_metadata.values()
                 if data.get("mesh_name") == self.mesh_name and data.get("mesh_address") == mesh_addr]
        macs = sorted({data["mac_address"] for data in nodes})
        result: dict[str, Any] = {"mesh_address": mesh_addr, "mesh_name": self.mesh_name,
                                  "mac_addresses": macs, "address_collision": len(macs) > 1}
        if len(nodes) == 1:
            result.update(nodes[0])
        result["mac_address"] = macs[0] if len(macs) == 1 else None
        result["mac_address_source"] = "advertisement" if macs else None
        result["mac_address_status"] = "matched" if len(macs) == 1 else "ambiguous" if macs else "not_found"
        type_code = result.get('device_type')
        source = 'advertisement' if type_code is not None else None
        result['device_type'] = type_code
        result['device_type_name'] = device_type_name(type_code)
        result['device_type_hex'] = f'0x{type_code:04X}' if isinstance(type_code, int) and 0 <= type_code <= 0xFFFF else None
        result['device_type_source'] = source
        if len(macs) <= 1 and mesh_addr in self.firmware_names:
            result["firmware_name"] = self.firmware_names[mesh_addr]
        name = result.get("firmware_name")
        version = result.get("firmware_version")
        result["firmware"] = f"{name}_{version}" if name and version else name or version
        result["online"] = self.light_states.get(mesh_addr, {}).get("online")
        result['mesh_group_addresses'] = sorted(self.light_groups.get(mesh_addr, set()))
        result['group_membership_verified_at'] = self.group_membership_verified.get(mesh_addr)
        imported = getattr(self, 'imported_devices', {}).get(str(mesh_addr))
        if imported:
            result['app_import_name'] = imported['name']
            result['app_import_device_type'] = imported['device_type']
            result['app_import_device_type_name'] = device_type_name(imported['device_type'])
            result['app_import_mac_address'] = imported.get('mac_address')
            result['app_import_source'] = 'legacy_app_share_settings'
        result['group_membership_source'] = ('device_report' if self.group_membership_verified.get(mesh_addr)
                                             else 'app_import' if mesh_addr in self.light_groups else None)
        return result

    def _persist_groups(self) -> None:
        options = dict(self.entry.options)
        options[CONF_MESH_GROUPS] = {
            'mesh_name': self.mesh_name,
            'groups': {str(address): dict(group) for address, group in self.mesh_groups.items()},
            'memberships': {str(address): {'groups': sorted(groups),
                                          'verified_at': self.group_membership_verified.get(address, 0)}
                            for address, groups in self.light_groups.items()},
        }
        self.hass.config_entries.async_update_entry(self.entry, options=options)

    def configure_group(self, group_address: int, name: str | None = None) -> None:
        """Name/create a local group control without changing device membership."""
        address = validate_group_address(group_address)
        if name is not None and not name.strip():
            raise ValueError('Enter a group name')
        is_new = address not in self.mesh_groups
        self.mesh_groups.setdefault(address, {'name': f'Group {address & 0xFF}'})
        if name is not None:
            self.mesh_groups[address]['name'] = name.strip()
        self._persist_groups()
        if is_new and self._new_group_callback:
            self._new_group_callback(address)
        self._notify_state_change(address)

    def get_group_members(self, group_address: int) -> set[int]:
        return {address for address, groups in self.light_groups.items()
                if group_address in groups and address in self.enrolled_mesh_addresses
                and not self.is_excluded_address(address) and not self.is_infrastructure_address(address)}

    def get_group_metadata(self, group_address: int) -> dict[str, Any]:
        members = self.get_group_members(group_address)
        return {'mesh_name': self.mesh_name, 'mesh_group_address': group_address,
                'group_name': self.mesh_groups[group_address]['name'],
                'member_addresses': sorted(members),
                'state_basis': ('reported_members' if all(self.group_membership_verified.get(member)
                                                          for member in members) else 'app_import'),
                **aggregate_group_state(members, self.light_states)}

    async def async_query_groups(self, mesh_address: int) -> set[int] | None:
        """Read membership for one enrolled light; timeout is not an empty group list."""
        if mesh_address not in self.enrolled_mesh_addresses or not self._is_controllable_light_address(mesh_address):
            raise HomeAssistantError('Select an enrolled light')
        if self.get_device_metadata(mesh_address)['address_collision']:
            raise HomeAssistantError('Resolve the known duplicate device address before querying membership')
        if mesh_address in self._group_reply_pending:
            raise HomeAssistantError('A membership query is already pending for this light')
        future = asyncio.get_running_loop().create_future()
        self._group_reply_pending[mesh_address] = future
        try:
            if not await self._async_send_command(mesh_address, self.protocol.generate_group_query,
                                                  'query groups', burst_count=1):
                return None
            async with asyncio.timeout(6):
                return await future
        except TimeoutError:
            return None
        finally:
            self._group_reply_pending.pop(mesh_address, None)
            if not future.done():
                future.cancel()

    async def async_set_group_membership(self, mesh_address: int, group_address: int, add: bool) -> None:
        """Change one light's membership, then verify without assuming success."""
        group_address = validate_group_address(group_address)
        if mesh_address not in self.enrolled_mesh_addresses or not self._is_controllable_light_address(mesh_address):
            raise HomeAssistantError('Select an enrolled light')
        if self.get_device_metadata(mesh_address)['address_collision']:
            raise HomeAssistantError('Resolve the known duplicate device address before changing membership')
        async with self._group_membership_lock:
            self.last_group_operation = {'mesh_address': mesh_address, 'group_address': group_address,
                                         'operation': 'add' if add else 'remove', 'status': 'pending'}
            try:
                if not await self._async_send_command(mesh_address,
                        lambda: self.protocol.generate_group_membership(group_address, add),
                        'add group membership' if add else 'remove group membership', burst_count=1):
                    raise HomeAssistantError('Group membership command could not be sent')
                await asyncio.sleep(0.5)
                groups = await self.async_query_groups(mesh_address)
                if groups is None or (group_address in groups) != add:
                    raise HomeAssistantError('Group change is unconfirmed; read membership before retrying')
                self.last_group_operation['status'] = 'verified'
            except Exception:
                self.last_group_operation['status'] = 'unconfirmed'
                raise

    def is_excluded_address(self, mesh_addr: int) -> bool:
        if mesh_addr in self.excluded_mesh_addresses:
            return True
        imported = getattr(self, 'imported_devices', {}).get(str(mesh_addr), {})
        if imported.get('mac_address') in self.excluded_mac_addresses:
            return True
        # If addresses collide, an excluded physical node makes this address ambiguous.
        return any(mac in self.excluded_mac_addresses and data.get("mesh_address") == mesh_addr
                   and data.get("mesh_name") == self.mesh_name
                   for mac, data in self.device_metadata.items())

    def apply_exclusions(self, addresses: set[int], macs: set[str]) -> None:
        """Remove excluded lights and prevent subsequent discovery from restoring them."""
        self.excluded_mesh_addresses = addresses
        self.excluded_mac_addresses = macs
        for address in list(self.cached_light_mesh_addresses):
            if self.is_excluded_address(address):
                self.pending_discovered_devices.pop(address, None)
                self.remove_cached_light_address(address)
                self._remove_light_entity_and_device(address)

    async def async_manage_device(self, mac: str, operation: str, new_name: str | None = None,
                                  new_password: str = "2846") -> dict[str, Any]:
        """Pause gateway traffic while directly managing one selected device."""
        mac = normalize_mac(mac)
        devices = self.refresh_advertisement_metadata()
        device = devices.get(mac)
        if device is None or device.get("mesh_name") != self.mesh_name:
            raise HomeAssistantError("Select a visible device on this hub's current network")
        if operation == "move" and new_name == self.mesh_name:
            raise HomeAssistantError("Select a different destination network")
        if operation == "move" and device.get("mesh_address"):
            if any(node.get("mesh_name") == new_name and node.get("mesh_address") == device["mesh_address"]
                   and other_mac != mac for other_mac, node in devices.items()):
                raise HomeAssistantError("A visible destination device already uses this mesh address; resolve the collision before moving")
        async with self._connect_lock:
            self._management_active = True
            try:
                async with self._command_lock:
                    await self.async_disconnect()
                    self.last_device_operation = {"mac_address": mac, "operation": operation, "status": "in_progress"}
                    self._notify_state_change(BROADCAST_ADDRESS)
                    result = await manage_device(self.hass, mac, self.mesh_name, self.password,
                                                 operation, new_name, new_password)
                    saved = {**self.device_metadata.get(mac, {}), **device}
                    if operation == "inspect":
                        saved["firmware_version"] = result["firmware_version"]
                    else:
                        address = saved.get("mesh_address")
                        if address and len(self.get_device_metadata(address)["mac_addresses"]) == 1:
                            self.remove_cached_light_address(address)
                            self._remove_light_entity_and_device(address)
                        saved["mesh_name"] = result["mesh_name"]
                        saved["pending_network_change"] = True
                        saved["pending_network_change_until"] = time.time() + NETWORK_CHANGE_ADVERTISEMENT_GRACE
                    self.device_metadata[mac] = saved
                    self._persist_device_metadata()
                    self.last_device_operation = result
                    if device.get("mesh_address"):
                        self._notify_state_change(device["mesh_address"])
                    return result
            except Exception as err:
                self.last_device_operation = {"mac_address": mac, "operation": operation, "status": "failed_or_uncertain"}
                _LOGGER.warning("Selected-device %s for %s failed or is uncertain: %s", operation, mac, err)
                raise
            finally:
                self._management_active = False
                self._notify_state_change(BROADCAST_ADDRESS)

    async def _async_validate_pending_light(self, mesh_addr: int) -> None:
        """Ask a pending mesh address for full status before creating an entity."""
        try:
            _LOGGER.debug(
                "Validating pending mesh light %d with direct status query",
                mesh_addr,
            )
            await self.async_query_status(mesh_addr)
            await asyncio.sleep(1.0)
            if mesh_addr in self.discovered_devices and self._is_controllable_light_address(mesh_addr):
                if mesh_addr not in self._firmware_requested:
                    await self.async_query_firmware(mesh_addr)
                await self.async_query_groups(mesh_addr)
            if mesh_addr not in self.discovered_devices:
                _LOGGER.debug(
                    "Pending mesh light %d did not answer direct status query",
                    mesh_addr,
                )
        except HomeAssistantError as err:
            _LOGGER.debug('Discovery metadata query for mesh light %d stopped: %s', mesh_addr, err)
        finally:
            pending = self.pending_discovered_devices.get(mesh_addr)
            if pending:
                pending.pop('validating', None)
            task = asyncio.current_task()
            if task is not None:
                self._discovery_validation_tasks.discard(task)

    def _schedule_pending_light_validation(self, mesh_addr: int) -> None:
        """Schedule bounded validation for a pending mesh light address."""
        if not self._discovery_allows(mesh_addr):
            return
        pending = self.pending_discovered_devices.get(mesh_addr)
        if not pending or pending.get('validating'):
            return

        now = time.time()
        last_validation = pending.get('last_validation', 0)
        if now - last_validation < DISCOVERY_VALIDATION_COOLDOWN:
            _LOGGER.debug(
                "Pending mesh light %d was recently validated; leaving pending",
                mesh_addr,
            )
            return

        if len(self._discovery_validation_tasks) >= MAX_DISCOVERY_VALIDATION_TASKS:
            _LOGGER.debug(
                "Discovery validation queue full; leaving mesh light %d pending",
                mesh_addr,
            )
            return

        pending['validating'] = True
        pending['last_validation'] = now
        task = self.entry.async_create_background_task(
            self.hass, self._async_validate_pending_light(mesh_addr), f'btalliance_validate_{mesh_addr}')
        self._discovery_validation_tasks.add(task)

    def remove_cached_light_address(self, mesh_addr: int, *, persist: bool = True) -> None:
        """Forget a light mesh address after the entity is removed."""
        self.enrolled_mesh_addresses.discard(mesh_addr)
        if mesh_addr not in self.cached_light_mesh_addresses:
            return

        self.cached_light_mesh_addresses.remove(mesh_addr)
        self.discovered_devices.pop(mesh_addr, None)
        self.validated_light_mesh_addresses.discard(mesh_addr)
        self.light_states.pop(mesh_addr, None)
        if persist:
            self._persist_cached_light_addresses()
        _LOGGER.info("Removed BTAlliance mesh light address %d from cache", mesh_addr)

    def forget_light_address(self, mesh_addr: int) -> None:
        """Remove both cached discovery and approval, retaining descriptive metadata."""
        self.remove_cached_light_address(mesh_addr, persist=False)
        self._persist_cached_light_addresses(persist_enrollment=True)

    def _remove_light_entity_and_device(self, mesh_addr: int) -> None:
        """Remove Home Assistant registry entries for a light mesh address."""
        unique_id = f"{self.entry.entry_id}_{mesh_addr}"
        entity_registry = er.async_get(self.hass)
        entity_id = entity_registry.async_get_entity_id("light", DOMAIN, unique_id)
        if entity_id:
            _LOGGER.info(
                "Removing BTAlliance light entity for mesh address %d: %s",
                mesh_addr,
                entity_id,
            )
            entity_registry.async_remove(entity_id)

        device_registry = dr.async_get(self.hass)
        device = device_registry.async_get_device(identifiers={(DOMAIN, unique_id)})
        if device:
            _LOGGER.info(
                "Removing BTAlliance light device for mesh address %d: %s",
                mesh_addr,
                device.id,
            )
            device_registry.async_remove_device(device.id)

    def cleanup_cached_light_addresses(
        self,
        keep_addresses: set[int] | None = None,
        require_validation: bool = True,
    ) -> list[int]:
        """Remove cached lights that were not confirmed as real mesh lights."""
        keep = set(keep_addresses or set())
        keep.difference_update(self.infrastructure_mesh_addresses)

        if require_validation:
            keep.update(self.validated_light_mesh_addresses)
        else:
            keep.update(self.discovered_devices)

        remove_addresses = sorted(self.cached_light_mesh_addresses - keep)
        for mesh_addr in remove_addresses:
            self.remove_cached_light_address(mesh_addr, persist=False)
            self._remove_light_entity_and_device(mesh_addr)

        if remove_addresses:
            self._persist_cached_light_addresses(persist_enrollment=True)

        if remove_addresses:
            _LOGGER.info(
                "Cleaned up %d unvalidated BTAlliance mesh light addresses: %s",
                len(remove_addresses),
                remove_addresses,
            )
        else:
            _LOGGER.info("No unvalidated BTAlliance mesh light addresses to clean up")

        return remove_addresses
    
    def register_state_callback(self, mesh_addr: int, callback: Callable) -> None:
        """Register callback for state updates for a specific mesh address."""
        self._state_callbacks.setdefault(mesh_addr, []).append(callback)
    
    def unregister_state_callback(self, mesh_addr: int, callback: Callable | None = None) -> None:
        """Unregister state callback."""
        if callback is None:
            self._state_callbacks.pop(mesh_addr, None)
            return

        callbacks = self._state_callbacks.get(mesh_addr)
        if not callbacks:
            return

        with contextlib.suppress(ValueError):
            callbacks.remove(callback)

        if not callbacks:
            self._state_callbacks.pop(mesh_addr, None)
    
    def _notify_state_change(self, mesh_addr: int) -> None:
        """Notify registered callback of state change."""
        callbacks = list(self._state_callbacks.get(mesh_addr, []))
        for group in self.mesh_groups:
            if mesh_addr in self.get_group_members(group):
                callbacks.extend(self._state_callbacks.get(group, []))
        # Also notify broadcast listeners
        if mesh_addr != BROADCAST_ADDRESS:
            callbacks.extend(self._state_callbacks.get(BROADCAST_ADDRESS, []))

        for callback in callbacks:
            callback()

    def is_infrastructure_address(self, mesh_addr: int) -> bool:
        """Return True if a mesh address is infrastructure, not a light."""
        return mesh_addr in self.infrastructure_mesh_addresses or any(
            node.get('mesh_name') == self.mesh_name and node.get('mesh_address') == mesh_addr
            and is_relay_device(node) for node in self.device_metadata.values())

    def _is_controllable_light_address(self, mesh_addr: int) -> bool:
        """Return True if a mesh address should be represented as a light."""
        if self.is_excluded_address(mesh_addr):
            return False
        if self.is_infrastructure_address(mesh_addr):
            self._remove_infrastructure_light_entity(mesh_addr)
            return False
        return 1 <= mesh_addr <= 254

    def _process_notification(self, data: bytearray) -> None:
        """Process incoming notification data."""
        parsed = self.protocol.parse_notification(data)
        if parsed is None:
            return
        
        opcode = parsed['opcode']

        if opcode == NOTIFY_GROUPS:
            address = parsed['source']
            future = self._group_reply_pending.get(address)
            if future is None or future.done() or address not in self.enrolled_mesh_addresses:
                return
            groups = set(parsed['group_addresses'])
            if not parsed['groups_complete']:
                # Retain existing memberships when the reply might be truncated.
                self.light_groups.setdefault(address, set()).update(groups)
            else:
                self.light_groups[address] = groups
                self.group_membership_verified[address] = time.time()
            for group in groups:
                if group not in self.mesh_groups:
                    self.configure_group(group)
            self._persist_groups()
            for group in self.mesh_groups:
                self._notify_state_change(group)
            self._notify_state_change(address)
            future.set_result(groups if parsed['groups_complete'] else None)
            return

        if opcode == NOTIFY_FIRMWARE:
            address = parsed['source']
            name = parsed.get('firmware_name')
            if not name or not 1 <= address <= 254 or self.is_excluded_address(address):
                return
            self.firmware_names[address] = name
            macs = self.get_device_metadata(address)["mac_addresses"]
            for metadata in self.device_metadata.values():
                if len(macs) > 1:
                    break
                if metadata.get("mesh_name") == self.mesh_name and metadata.get("mesh_address") == address:
                    metadata["firmware_name"] = name
            self._persist_device_metadata()
            self._notify_state_change(address)
            return
        
        if opcode == NOTIFY_STATUS_RESPONSE:
            # Full status response (0xDB)
            target_addr = self.protocol.get_target_address()
            source_addr = parsed['source']
            if not 1 <= source_addr <= 254:
                return
            response_addr = source_addr
            self.observed_mesh_addresses[response_addr] = {'last_seen': time.time(), 'online': True}
            if not self._discovery_allows(response_addr) and not self.is_infrastructure_address(response_addr):
                return

            if target_addr != BROADCAST_ADDRESS and response_addr != target_addr:
                _LOGGER.debug(
                    "Ignoring 0xDB status for source %d while querying addr %d raw=%s",
                    response_addr,
                    target_addr,
                    parsed['raw'].hex(),
                )
                return

            if self.is_infrastructure_address(response_addr):
                self.infrastructure_states[response_addr] = {'last_seen': time.time()}
                self._notify_state_change(response_addr)
                return

            if not self._is_controllable_light_address(response_addr):
                _LOGGER.debug(
                    "Ignoring 0xDB status for non-light source %d target=%d raw=%s",
                    response_addr,
                    target_addr,
                    parsed['raw'].hex(),
                )
                return

            state = {
                'is_on': parsed['is_on'],
                'luminance': parsed['luminance'],
                'red': parsed['red'],
                'green': parsed['green'],
                'blue': parsed['blue'],
                'color_temp': parsed['color_temp'],
                'warm': parsed['warm'],
                'cool': parsed['cool'],
                'color_mode': 'rgb'
                if any((parsed['red'], parsed['green'], parsed['blue']))
                else 'color_temp',
                'last_seen': time.time(),
                'online': True,
            }
            self.validated_light_mesh_addresses.add(response_addr)
            self.light_states[response_addr] = state
            _LOGGER.debug(
                "0xDB status for addr %d target=%d: on=%s lum=%d RGB=(%d,%d,%d) CT=%d warm=%d cool=%d raw=%s",
                          response_addr, target_addr, parsed['is_on'], parsed['luminance'],
                          parsed['red'], parsed['green'], parsed['blue'],
                          parsed['color_temp'], parsed['warm'], parsed['cool'],
                          parsed['raw'].hex())
            if (
                response_addr in self.pending_discovered_devices
                or response_addr not in self.discovered_devices
            ):
                self._confirm_light_address(
                    response_addr,
                    state,
                    "broadcast status response"
                    if target_addr == BROADCAST_ADDRESS
                    else "direct status response",
                )
                return

            if response_addr in self.cached_light_mesh_addresses:
                self.discovered_devices[response_addr] = state

            self._notify_state_change(response_addr)
            
        elif opcode == NOTIFY_LIGHT_STATUS:
            for record in parsed.get('records', []):
                self._process_mesh_status({**parsed, **record})

    def _process_mesh_status(self, parsed: dict[str, Any]) -> None:
        """Apply one status record without inferring device ownership."""
        address = parsed['light_addr']
        now = time.time()
        online = parsed['online']
        self.observed_mesh_addresses[address] = {
            'last_seen': now, 'online': online,
            'status_counter': parsed['status_counter'],
        }
        if self.is_infrastructure_address(address):
            is_new = address not in self.infrastructure_states
            self.infrastructure_states.setdefault(address, {})['online'] = online
            if online:
                self.infrastructure_states[address]['last_seen'] = now
                if is_new and self._new_infrastructure_callback:
                    self._new_infrastructure_callback(address)
            self._notify_state_change(address)
            return
        if not online:
            self.pending_discovered_devices.pop(address, None)
            if address in self.light_states:
                self.light_states[address]['online'] = False
                self._notify_state_change(address)
            return
        if not self._discovery_allows(address) or not self._is_controllable_light_address(address):
            _LOGGER.debug("Observed unenrolled mesh address %d; not creating or querying a light", address)
            return
        if address not in self.enrolled_mesh_addresses:
            pending = self.pending_discovered_devices.setdefault(address, {'reports': 0})
            if pending.get('last_counter') != parsed['status_counter']:
                pending['reports'] += 1
                pending['last_counter'] = parsed['status_counter']
            if pending['reports'] >= MESH_LIGHT_CONFIRMATION_COUNT:
                self._confirm_light_address(address, {'online': True, 'is_on': parsed['is_on'],
                    'luminance': parsed['luminance'], 'last_seen': now}, 'repeated online reports')
            else:
                self._schedule_pending_light_validation(address)
            return
        state = self.light_states.setdefault(address, {})
        state.update({'online': True, 'is_on': parsed['is_on'],
                      'luminance': parsed['luminance'], 'last_seen': now})
        if address in self.cached_light_mesh_addresses:
            self.discovered_devices[address] = state
        _LOGGER.debug("0xDC mesh: light=%d online=%s lum=%d counter=%d src=%d mic=%s raw=%s",
                      address, online, parsed['luminance'], parsed['status_counter'],
                      parsed['source'], parsed['mic'], parsed['raw'].hex())
        self._notify_state_change(address)

    def _candidate_gateway_addresses(self) -> list[str]:
        """Return configured gateway first, then other discovered Fulife nodes."""
        configured_mac = self._format_mac(self.gateway_address)
        visible_devices = self.refresh_advertisement_metadata()
        now = time.time()
        candidates: dict[str, int] = {configured_mac: 0}
        candidate_info: dict[str, dict[str, Any]] = {
            configured_mac: {
                "address": configured_mac,
                "name": None,
                "rssi": None,
                "configured": True,
                "preferred_relay": is_relay_device(visible_devices.get(configured_mac, self.device_metadata.get(configured_mac, {}))),
                "last_failure": self.gateway_candidate_failures.get(configured_mac),
            }
        }

        for service_info in bluetooth.async_discovered_service_info(self.hass):
            node = advertisement(service_info)
            if not node or node.get("mesh_name") != self.mesh_name:
                continue

            rssi = service_info.rssi or -999
            candidates.setdefault(service_info.address, rssi)
            candidate_info[service_info.address] = {
                "address": service_info.address,
                "name": service_info.name,
                "rssi": service_info.rssi,
                "configured": service_info.address == configured_mac,
                "preferred_relay": is_relay_device(node),
                "device_type_name": device_type_name(node.get('device_type')),
                "last_failure": self.gateway_candidate_failures.get(service_info.address),
            }

        self.gateway_candidates = candidate_info
        candidates_to_try = []
        for address in candidates:
            visible = visible_devices.get(address)
            if visible and visible.get("mesh_name") and visible["mesh_name"] != self.mesh_name:
                continue
            if address in self.excluded_mac_addresses:
                continue
            saved = self.device_metadata.get(address, {})
            if saved.get("mesh_name") and saved["mesh_name"] != self.mesh_name:
                continue
            if saved.get("mesh_address") and self.is_excluded_address(saved["mesh_address"]):
                continue
            failure = self.gateway_candidate_failures.get(address, {})
            failed_at = failure.get("time", 0)
            reason = failure.get("reason")
            elapsed = now - failed_at

            if address == configured_mac:
                if elapsed >= CONFIGURED_GATEWAY_FAILURE_BACKOFF:
                    candidates_to_try.append(address)
                continue

            backoff = (
                FALLBACK_GATEWAY_SESSION_BACKOFF
                if reason == "session_setup_failed"
                else FALLBACK_GATEWAY_CONNECT_BACKOFF
            )
            if elapsed >= backoff:
                candidates_to_try.append(address)

        sorted_candidates = sorted(
            candidates_to_try,
            key=lambda address: (
                address != configured_mac,
                not candidate_info[address].get('preferred_relay', False),
                -(candidates[address] or -999),
            ),
        )

        if configured_mac in sorted_candidates:
            fallback_candidates = [
                address for address in sorted_candidates if address != configured_mac
            ][:MAX_FALLBACK_GATEWAY_CANDIDATES]
            return [configured_mac, *fallback_candidates]

        return sorted_candidates[:MAX_FALLBACK_GATEWAY_CANDIDATES]

    def _mark_gateway_candidate_failed(self, mac_str: str, reason: str) -> None:
        """Remember a failed candidate briefly so retries can try other devices."""
        self.gateway_candidate_failures[mac_str] = {
            "time": time.time(),
            "reason": reason,
        }

    def _clear_gateway_candidate_failure(self, mac_str: str) -> None:
        """Clear any remembered failure for a successful candidate."""
        self.gateway_candidate_failures.pop(mac_str, None)
    
    async def async_connect(self) -> bool:
        """Connect to the gateway device and establish session."""
        async with self._connect_lock:
            if self._management_active:
                return False
            if self.login_valid and self.client and self.client.is_connected:
                return True

            for mac_str in self._candidate_gateway_addresses():
                _LOGGER.info("Connecting to gateway candidate: %s", mac_str)

                # Find the BLE device using HA's bluetooth integration
                self.ble_device = async_ble_device_from_address(
                    self.hass,
                    mac_str,
                    connectable=True
                )

                if self.ble_device is None:
                    _LOGGER.warning("Gateway candidate not found: %s", mac_str)
                    continue

                _LOGGER.debug("Found BLE device: %s", self.ble_device)

                try:
                    # Use bleak_retry_connector for robust connection via ESPHome proxy
                    async with asyncio.timeout(CONNECT_ATTEMPT_TIMEOUT):
                        self.client = await establish_connection(
                            BleakClientWithServiceCache,
                            self.ble_device,
                            mac_str,
                            disconnected_callback=self._on_disconnect,
                        )
                    self.connected = True
                    self.selected_gateway_address = mac_str
                    self.selected_gateway_name = self.gateway_candidates.get(mac_str, {}).get("name")
                    self.selected_gateway_rssi = self.gateway_candidates.get(mac_str, {}).get("rssi")
                    self.mac_bytes = _mac_to_int(mac_str).to_bytes(6, "little")
                    self.protocol = TelinkProtocol(self.mac_bytes, self.mesh_name, self.password)
                    _LOGGER.info("Connected to gateway candidate: %s", self.ble_device.address)
                    if await self._async_setup_session():
                        self._clear_gateway_candidate_failure(mac_str)
                        self._notify_state_change(BROADCAST_ADDRESS)
                        return True

                    _LOGGER.warning("Gateway candidate %s failed session setup", mac_str)
                    self._mark_gateway_candidate_failed(mac_str, "session_setup_failed")
                    await self.async_disconnect()
                except TimeoutError:
                    _LOGGER.warning(
                        "Timed out connecting to gateway candidate %s after %.1fs",
                        mac_str,
                        CONNECT_ATTEMPT_TIMEOUT,
                    )
                    self._mark_gateway_candidate_failed(mac_str, "connect_timeout")
                except BleakError as e:
                    _LOGGER.warning("Failed to connect to gateway candidate %s: %s", mac_str, e)
                    self._mark_gateway_candidate_failed(mac_str, str(e))
                except Exception as e:
                    _LOGGER.warning("Unexpected connection error for gateway candidate %s: %s", mac_str, e)
                    self._mark_gateway_candidate_failed(mac_str, str(e))

            _LOGGER.error("Failed to connect to any usable Fulife gateway candidate")
            return False

    async def _async_setup_session(self) -> bool:
        """Log in to the connected candidate and enable mesh notifications."""
        # Login
        try:
            session_random = bytearray(os.urandom(8))
            
            login_data = bytearray(17)
            login_data[0] = 0x0C
            login_payload = self.protocol.generate_login_payload(session_random)
            login_data[1:17] = login_payload
            
            _LOGGER.debug("Sending login request...")
            await self.client.write_gatt_char(START_SESSION_UUID_STR, bytes(login_data), response=True)
            await asyncio.sleep(0.1)
            
            # Read response
            response = await self.client.read_gatt_char(START_SESSION_UUID_STR)
            _LOGGER.debug("Login response: %s", response.hex() if response else "None")
            
            self.login_valid = self.protocol.process_login_response(response, session_random)
            
            if not self.login_valid:
                _LOGGER.error("Login failed - invalid response")
                return False
            
            _LOGGER.info("Login successful to gateway %s", self.ble_device.address)
            
        except Exception as e:
            _LOGGER.error("Login error: %s", e)
            import traceback
            _LOGGER.debug(traceback.format_exc())
            return False
        
        # Setup notifications
        try:
            _LOGGER.debug("Enabling notifications...")
            await self.client.start_notify(NOTIFY_UUID_STR, self._on_notification)
            await self.client.write_gatt_char(NOTIFY_UUID_STR, bytes([0x01]), response=True)
            _LOGGER.debug("Notifications enabled")
        except Exception as e:
            _LOGGER.warning("Failed to enable notifications: %s", e)
            return False
        
        # Send datetime command
        try:
            async with self._command_lock:
                datetime_cmd = self.protocol.generate_datetime_command()
                await self.client.write_gatt_char(COMMAND_UUID_STR, bytes(datetime_cmd), response=True)
            _LOGGER.debug("DateTime command sent")
        except Exception as e:
            _LOGGER.warning("Failed to send datetime: %s", e)
        
        return True
    
    def _on_disconnect(self, client: BleakClient) -> None:
        """Handle disconnection."""
        if client is not self.client:
            _LOGGER.debug("Ignoring disconnect from stale gateway client")
            return

        _LOGGER.warning("Disconnected from gateway")
        self.connected = False
        self.login_valid = False
        self._notify_state_change(BROADCAST_ADDRESS)
    
    def _on_notification(self, sender, data: bytearray) -> None:
        """Handle incoming BLE notification."""
        _LOGGER.debug("Notification received: %s", data.hex() if data else "None")
        self._process_notification(bytearray(data))
    
    async def async_disconnect(self) -> None:
        """Disconnect from gateway."""
        if self.client and self.client.is_connected:
            await self.client.disconnect()
        self.client = None
        self.connected = False
        self.login_valid = False
        self.selected_gateway_address = None
        self.selected_gateway_name = None
        self.selected_gateway_rssi = None
        _LOGGER.debug("Disconnected from gateway")
        self._notify_state_change(BROADCAST_ADDRESS)
    
    async def async_discover_mesh_devices(self, timeout: float = MESH_DISCOVERY_TIMEOUT) -> Dict[int, Dict]:
        """Discover mesh devices by sending broadcast and waiting for 0xDC responses."""
        if not self.login_valid:
            _LOGGER.error("Cannot discover - not logged in")
            return {}
        
        _LOGGER.info("Starting mesh device discovery (timeout=%.1fs)...", timeout)
        
        # Don't clear - keep devices discovered during connection
        
        try:
            # Send multiple query commands to ensure all devices respond
            for i in range(3):
                _LOGGER.debug("Sending broadcast query command %d/3...", i + 1)
                await self._async_send_command(
                    BROADCAST_ADDRESS,
                    self.protocol.generate_query_status_command,
                    "discovery broadcast query",
                    burst_count=1,
                )
                await asyncio.sleep(1.0)  # Wait between commands
            
            # Wait additional time for responses
            remaining = timeout - 3.0
            if remaining > 0:
                _LOGGER.debug("Waiting %.1fs for mesh responses...", remaining)
                await asyncio.sleep(remaining)
                
        except Exception as e:
            _LOGGER.error("Discovery command failed: %s", e)
            import traceback
            _LOGGER.debug(traceback.format_exc())
        
        _LOGGER.info("Discovered %d mesh devices: %s", 
                    len(self.discovered_devices), 
                    list(self.discovered_devices.keys()))
        
        return self.discovered_devices.copy()
    
    async def _async_send_command(
        self,
        mesh_addr: int,
        command_factory: Callable[[], bytearray],
        description: str,
        burst_count: int = COMMAND_BURST_COUNT,
    ) -> bool:
        """Build and send a command to a specific mesh address."""
        if self._management_active or self.is_excluded_address(mesh_addr):
            return False
        if not self.login_valid:
            _LOGGER.error("Cannot send command - not logged in")
            return False

        if self.client is None:
            _LOGGER.error("Cannot send command - no BLE client")
            return False

        async with self._command_lock:
            if not self.login_valid or self.client is None:
                _LOGGER.error("Cannot send command - connection is no longer logged in")
                return False

            self.protocol.set_target_address(mesh_addr)

            try:
                if "query" in description:
                    delay = 1.0 - (time.monotonic() - self._last_query_sent)
                    if delay > 0:
                        await asyncio.sleep(delay)
                    self._last_query_sent = time.monotonic()
                for attempt in range(1, burst_count + 1):
                    command_data = command_factory()
                    _LOGGER.debug(
                        "Sending %s to mesh addr %d (%d/%d): %s",
                        description,
                        mesh_addr,
                        attempt,
                        burst_count,
                        command_data.hex(),
                    )
                    await self.client.write_gatt_char(
                        COMMAND_UUID_STR,
                        bytes(command_data),
                        response=COMMAND_WRITE_WITH_RESPONSE,
                    )
                    if attempt < burst_count:
                        await asyncio.sleep(COMMAND_BURST_DELAY)
                return True
            except Exception as e:
                _LOGGER.error("Command send failed: %s", e)
        
        return False
    
    async def async_turn_on(self, mesh_addr: int) -> bool:
        """Turn on without changing individual or member brightness levels."""
        return await self._async_send_command(
            mesh_addr,
            lambda: self.protocol.generate_on_off_command(True),
            "turn on",
        )
    
    async def async_turn_off(self, mesh_addr: int) -> bool:
        """Turn off light at mesh address."""
        return await self._async_send_command(
            mesh_addr,
            lambda: self.protocol.generate_on_off_command(False),
            "turn off",
        )
    
    async def async_set_brightness(self, mesh_addr: int, brightness: int) -> bool:
        """Set brightness (0-255 HA scale, converted to 0-100)."""
        level = int(brightness * 100 / 255)
        return await self._async_send_command(
            mesh_addr,
            lambda: self.protocol.generate_luminance_command(level),
            "set brightness",
        )
    
    async def async_set_rgb(self, mesh_addr: int, red: int, green: int, blue: int) -> bool:
        """Set RGB color."""
        return await self._async_send_command(
            mesh_addr,
            lambda: self.protocol.generate_rgb_command(red, green, blue),
            "set RGB",
        )
    
    async def async_set_color_temp(self, mesh_addr: int, color_temp_pct: int) -> bool:
        """Set color temperature (0=warm, 100=cool)."""
        return await self._async_send_command(
            mesh_addr,
            lambda: self.protocol.generate_color_temp_command(color_temp_pct),
            "set color temperature",
        )
    
    async def async_query_status(self, mesh_addr: int) -> bool:
        """Query status of specific device."""
        return await self._async_send_command(
            mesh_addr,
            self.protocol.generate_query_status_command,
            "query status",
            burst_count=1,
        )

    async def async_query_firmware(self, mesh_addr: int) -> bool:
        """Request identity without changing the device or creating an entity."""
        self._firmware_requested.add(mesh_addr)
        return await self._async_send_command(mesh_addr, self.protocol.generate_firmware_query, "query firmware", burst_count=1)

    async def async_broadcast_turn_on(self) -> bool:
        """Turn on all lights via mesh broadcast."""
        return await self.async_turn_on(BROADCAST_ADDRESS)

    async def async_broadcast_turn_off(self) -> bool:
        """Turn off all lights via mesh broadcast."""
        return await self.async_turn_off(BROADCAST_ADDRESS)

    async def async_broadcast_query_status(self) -> bool:
        """Request status from all mesh lights via broadcast."""
        return await self._async_send_command(
            BROADCAST_ADDRESS,
            self.protocol.generate_query_status_command,
            "broadcast status query",
            burst_count=1,
        )

    async def async_broadcast_sync_time(self) -> bool:
        """Sync date/time to all mesh lights via broadcast."""
        return await self._async_send_command(
            BROADCAST_ADDRESS,
            self.protocol.generate_datetime_command,
            "broadcast time sync",
        )

    async def async_broadcast_set_brightness(self, brightness: int) -> bool:
        """Set brightness for all lights via mesh broadcast."""
        return await self.async_set_brightness(BROADCAST_ADDRESS, brightness)

    async def async_broadcast_set_rgb(self, red: int, green: int, blue: int) -> bool:
        """Set RGB color for all lights via mesh broadcast."""
        return await self.async_set_rgb(BROADCAST_ADDRESS, red, green, blue)

    async def async_broadcast_set_color_temp(self, color_temp_pct: int) -> bool:
        """Set color temperature for all lights via mesh broadcast."""
        return await self.async_set_color_temp(BROADCAST_ADDRESS, color_temp_pct)

    def get_light_state(self, mesh_addr: int) -> Optional[Dict[str, Any]]:
        """Get cached state for a light."""
        if mesh_addr in self.mesh_groups:
            return aggregate_group_state(self.get_group_members(mesh_addr), self.light_states)
        return self.light_states.get(mesh_addr)

    def get_infrastructure_state(self, mesh_addr: int) -> Optional[Dict[str, Any]]:
        """Get cached state for a mesh infrastructure node."""
        return self.infrastructure_states.get(mesh_addr)

    def get_gateway_diagnostics(self) -> Dict[str, Any]:
        """Return diagnostic details for the currently selected BLE gateway."""
        configured_address = self._format_mac(self.gateway_address)
        return {
            "mesh_name": self.mesh_name,
            "discovered_networks": discovered_networks(self.hass),
            "discovered_bluetooth_devices": list(discovered_devices(self.hass).values()),
            "excluded_mesh_addresses": sorted(self.excluded_mesh_addresses),
            "excluded_mac_addresses": sorted(self.excluded_mac_addresses),
            "enrolled_mesh_addresses": sorted(self.enrolled_mesh_addresses),
            "discovery_mode": self.discovery_mode,
            "mesh_groups": {str(address): self.get_group_metadata(address) for address in self.mesh_groups},
            "last_group_operation": self.last_group_operation,
            "observed_mesh_addresses": {
                str(address): state for address, state in sorted(self.observed_mesh_addresses.items())
            },
            "unenrolled_mesh_addresses": sorted(set(self.observed_mesh_addresses) - self.enrolled_mesh_addresses - self.infrastructure_mesh_addresses),
            "last_device_operation": self.last_device_operation,
            "connected": self.connected,
            "logged_in": self.login_valid,
            "configured_gateway_address": configured_address,
            "selected_gateway_address": self.selected_gateway_address,
            "selected_gateway_name": self.selected_gateway_name,
            "selected_gateway_rssi": self.selected_gateway_rssi,
            "gateway_candidates": [
                self.gateway_candidates[address]
                for address in sorted(
                    self.gateway_candidates,
                    key=lambda item: (
                        item != configured_address,
                        not self.gateway_candidates[item].get('preferred_relay', False),
                        -(self.gateway_candidates[item].get("rssi") or -999),
                    ),
                )
            ],
        }
    
    @staticmethod
    def _format_mac(address: int) -> str:
        """Format integer address as MAC string."""
        addr_hex = f"{address:012X}"
        return ':'.join([addr_hex[i:i+2] for i in range(0, 12, 2)])
    
    async def _async_update_data(self) -> Dict[int, Dict[str, Any]]:
        """Fetch data from mesh network via periodic broadcast query."""
        if self._management_active:
            return self.light_states.copy()
        self.refresh_advertisement_metadata()
        self.apply_exclusions(self.excluded_mesh_addresses, self.excluded_mac_addresses)
        # Try to reconnect if not connected
        if not self.login_valid:
            _LOGGER.info("Not connected - attempting reconnection...")
            try:
                if await self.async_connect():
                    _LOGGER.info("Reconnection successful")
                else:
                    _LOGGER.warning("Reconnection failed - will retry next poll")
                    return self.light_states.copy()
            except Exception as e:
                _LOGGER.warning("Reconnection error: %s", e)
                return self.light_states.copy()
        
        _LOGGER.debug("Periodic status poll - sending broadcast query")
        
        try:
            await self._async_send_command(
                BROADCAST_ADDRESS,
                self.protocol.generate_query_status_command,
                "periodic broadcast query",
                burst_count=1,
            )
        except Exception as e:
            _LOGGER.warning("Periodic poll failed: %s - marking disconnected", e)
            self.login_valid = False
            self.connected = False
        
        return self.light_states.copy()
