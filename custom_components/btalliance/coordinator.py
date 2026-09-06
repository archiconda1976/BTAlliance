"""Coordinator for BTAlliance mesh device management."""

import asyncio
import logging
import os
import time
from typing import Any, Callable, Dict, Optional

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from bleak_retry_connector import establish_connection, BleakClientWithServiceCache
from bleak import BleakClient
from bleak.exc import BleakError

from datetime import timedelta

from .const import (
    DOMAIN,
    CONF_DISCOVERED_LIGHT_MESH_ADDRESSES,
    SERVICE_UUID, START_SESSION_UUID, NOTIFY_UUID, COMMAND_UUID,
    NOTIFY_STATUS_RESPONSE, NOTIFY_LIGHT_STATUS,
    MAX_CONNECTION_RETRIES, CONNECTION_TIMEOUT, LOGIN_TIMEOUT,
    DISCONNECT_TIMEOUT, RETRY_DELAY, MESH_DISCOVERY_TIMEOUT,
    BROADCAST_ADDRESS, POLLING_INTERVAL, FULIFE_MAC_PREFIXES,
)
from .protocol import TelinkProtocol

_LOGGER = logging.getLogger(__name__)

# UUID strings for bleak
SERVICE_UUID_STR = "00010203-0405-0607-0809-0a0b0c0d1910"
START_SESSION_UUID_STR = "00010203-0405-0607-0809-0a0b0c0d1914"
NOTIFY_UUID_STR = "00010203-0405-0607-0809-0a0b0c0d1911"
COMMAND_UUID_STR = "00010203-0405-0607-0809-0a0b0c0d1912"
CONNECT_ATTEMPT_TIMEOUT = 15.0
COMMAND_BURST_COUNT = 5
COMMAND_BURST_DELAY = 0.35
COMMAND_WRITE_WITH_RESPONSE = True


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
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=POLLING_INTERVAL),
        )
        
        self.entry = entry
        self.gateway_address = gateway_address
        self.mesh_name = mesh_name
        self.password = password
        self.infrastructure_mesh_addresses = infrastructure_mesh_addresses or set()
        self.cached_light_mesh_addresses = cached_light_mesh_addresses or set()
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
        self.start_session_handle: Optional[int] = None
        self.notify_handle: Optional[int] = None
        self.command_handle: Optional[int] = None
        
        # Mesh devices discovered via 0xDC notifications
        self.discovered_devices: Dict[int, Dict[str, Any]] = {}
        
        # Light state cache per mesh address
        self.light_states: Dict[int, Dict[str, Any]] = {}

        # Infrastructure node state cache per mesh address
        self.infrastructure_states: Dict[int, Dict[str, Any]] = {
            address: {'last_seen': None} for address in self.infrastructure_mesh_addresses
        }
        
        # Callbacks for state updates
        self._state_callbacks: Dict[int, Callable] = {}
        
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

    def _persist_cached_light_addresses(self) -> None:
        """Persist known light mesh addresses to the config entry options."""
        value = ",".join(str(address) for address in sorted(self.cached_light_mesh_addresses))
        options = dict(self.entry.options)
        options[CONF_DISCOVERED_LIGHT_MESH_ADDRESSES] = value
        self.hass.config_entries.async_update_entry(self.entry, options=options)

    def add_cached_light_address(self, mesh_addr: int) -> None:
        """Remember a light mesh address across restarts."""
        if not self._is_controllable_light_address(mesh_addr):
            return
        if mesh_addr in self.cached_light_mesh_addresses:
            return

        self.cached_light_mesh_addresses.add(mesh_addr)
        self._persist_cached_light_addresses()
        _LOGGER.info("Cached BTAlliance mesh light address %d", mesh_addr)

    def remove_cached_light_address(self, mesh_addr: int) -> None:
        """Forget a light mesh address after the entity is removed."""
        if mesh_addr not in self.cached_light_mesh_addresses:
            return

        self.cached_light_mesh_addresses.remove(mesh_addr)
        self.discovered_devices.pop(mesh_addr, None)
        self.light_states.pop(mesh_addr, None)
        self._persist_cached_light_addresses()
        _LOGGER.info("Removed BTAlliance mesh light address %d from cache", mesh_addr)
    
    def register_state_callback(self, mesh_addr: int, callback: Callable) -> None:
        """Register callback for state updates for a specific mesh address."""
        self._state_callbacks[mesh_addr] = callback
    
    def unregister_state_callback(self, mesh_addr: int) -> None:
        """Unregister state callback."""
        self._state_callbacks.pop(mesh_addr, None)
    
    def _notify_state_change(self, mesh_addr: int) -> None:
        """Notify registered callback of state change."""
        if mesh_addr in self._state_callbacks:
            self._state_callbacks[mesh_addr]()
        # Also notify broadcast listeners
        if BROADCAST_ADDRESS in self._state_callbacks:
            self._state_callbacks[BROADCAST_ADDRESS]()

    def is_infrastructure_address(self, mesh_addr: int) -> bool:
        """Return True if a mesh address is infrastructure, not a light."""
        return mesh_addr in self.infrastructure_mesh_addresses

    def _is_controllable_light_address(self, mesh_addr: int) -> bool:
        """Return True if a mesh address should be represented as a light."""
        if self.is_infrastructure_address(mesh_addr):
            return False
        return 1 <= mesh_addr <= 254
    
    def _process_notification(self, data: bytearray) -> None:
        """Process incoming notification data."""
        parsed = self.protocol.parse_notification(data)
        if parsed is None:
            return
        
        opcode = parsed['opcode']
        
        if opcode == NOTIFY_STATUS_RESPONSE:
            # Full status response (0xDB) - update state for current target
            target_addr = self.protocol.get_target_address()
            self.light_states[target_addr] = {
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
            }
            _LOGGER.debug(
                "0xDB status for addr %d: on=%s lum=%d RGB=(%d,%d,%d) CT=%d warm=%d cool=%d raw=%s",
                          target_addr, parsed['is_on'], parsed['luminance'],
                          parsed['red'], parsed['green'], parsed['blue'],
                          parsed['color_temp'], parsed['warm'], parsed['cool'],
                          parsed['raw'].hex())
            self._notify_state_change(target_addr)
            
        elif opcode == NOTIFY_LIGHT_STATUS:
            # Mesh broadcast status (0xDC)
            light_addr = parsed['light_addr']
            is_on = parsed['is_on']
            luminance = parsed['luminance']

            if self.is_infrastructure_address(light_addr):
                is_new_node = light_addr not in self.infrastructure_states
                self.infrastructure_states[light_addr] = {'last_seen': time.time()}
                _LOGGER.info(
                    "Mesh infrastructure node seen at address %d src=%d dst=%d raw=%s",
                    light_addr,
                    parsed['source'],
                    parsed['destination'],
                    parsed['raw'].hex(),
                )
                if is_new_node and self._new_infrastructure_callback:
                    self._new_infrastructure_callback(light_addr)
                self._notify_state_change(light_addr)
                return

            if not self._is_controllable_light_address(light_addr):
                _LOGGER.info(
                    "Ignoring mesh status for non-light address %d src=%d dst=%d raw=%s",
                    light_addr,
                    parsed['source'],
                    parsed['destination'],
                    parsed['raw'].hex(),
                )
                return
            
            # Check if this is a new device
            is_new_device = light_addr not in self.discovered_devices
            self.add_cached_light_address(light_addr)
            
            # Track discovered device
            self.discovered_devices[light_addr] = {
                'is_on': is_on,
                'luminance': luminance,
                'last_seen': time.time()
            }
            
            # Update light state if we have it
            if light_addr in self.light_states:
                self.light_states[light_addr]['is_on'] = is_on
                self.light_states[light_addr]['luminance'] = luminance
                self.light_states[light_addr]['last_seen'] = time.time()
            else:
                self.light_states[light_addr] = {
                    'is_on': is_on,
                    'luminance': luminance,
                    'last_seen': time.time(),
                }
            
            _LOGGER.debug("0xDC mesh: light=%d %s lum=%d src=%d dst=%d raw=%s (total: %d devices)",
                         light_addr, "ON" if is_on else "OFF", luminance,
                         parsed['source'], parsed['destination'], parsed['raw'].hex(),
                         len(self.discovered_devices))
            
            # Notify about new device discovery
            if is_new_device and self._new_device_callback:
                _LOGGER.info("New mesh device discovered: %d", light_addr)
                self._new_device_callback(light_addr)
            
            self._notify_state_change(light_addr)

    def _candidate_gateway_addresses(self) -> list[str]:
        """Return configured gateway first, then other discovered Fulife nodes."""
        configured_mac = self._format_mac(self.gateway_address)
        candidates: dict[str, int] = {configured_mac: 0}

        for service_info in bluetooth.async_discovered_service_info(self.hass):
            if not _is_fulife_service_info(service_info):
                continue

            rssi = service_info.rssi or -999
            candidates.setdefault(service_info.address, rssi)

        return sorted(
            candidates,
            key=lambda address: (
                address != configured_mac,
                -(candidates[address] or -999),
            ),
        )
    
    async def async_connect(self) -> bool:
        """Connect to the gateway device and establish session."""
        async with self._connect_lock:
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
                    self.mac_bytes = _mac_to_int(mac_str).to_bytes(6, "little")
                    self.protocol = TelinkProtocol(self.mac_bytes, self.mesh_name, self.password)
                    _LOGGER.info("Connected to gateway candidate: %s", self.ble_device.address)
                    if await self._async_setup_session():
                        return True

                    _LOGGER.warning("Gateway candidate %s failed session setup", mac_str)
                    await self.async_disconnect()
                except TimeoutError:
                    _LOGGER.warning(
                        "Timed out connecting to gateway candidate %s after %.1fs",
                        mac_str,
                        CONNECT_ATTEMPT_TIMEOUT,
                    )
                except BleakError as e:
                    _LOGGER.warning("Failed to connect to gateway candidate %s: %s", mac_str, e)
                except Exception as e:
                    _LOGGER.warning("Unexpected connection error for gateway candidate %s: %s", mac_str, e)

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
        _LOGGER.debug("Disconnected from gateway")
    
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
    ) -> bool:
        """Build and send a command to a specific mesh address."""
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
                for attempt in range(1, COMMAND_BURST_COUNT + 1):
                    command_data = command_factory()
                    _LOGGER.debug(
                        "Sending %s to mesh addr %d (%d/%d): %s",
                        description,
                        mesh_addr,
                        attempt,
                        COMMAND_BURST_COUNT,
                        command_data.hex(),
                    )
                    await self.client.write_gatt_char(
                        COMMAND_UUID_STR,
                        bytes(command_data),
                        response=COMMAND_WRITE_WITH_RESPONSE,
                    )
                    if attempt < COMMAND_BURST_COUNT:
                        await asyncio.sleep(COMMAND_BURST_DELAY)
                return True
            except Exception as e:
                _LOGGER.error("Command send failed: %s", e)
        
        return False
    
    async def async_turn_on(self, mesh_addr: int) -> bool:
        """Turn on light at mesh address."""
        if not await self._async_send_command(
            mesh_addr,
            lambda: self.protocol.generate_on_off_command(True),
            "turn on",
        ):
            return False

        await asyncio.sleep(0.25)
        return await self._async_send_command(
            mesh_addr,
            lambda: self.protocol.generate_luminance_command(100),
            "restore brightness",
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
        )

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
        return self.light_states.get(mesh_addr)

    def get_infrastructure_state(self, mesh_addr: int) -> Optional[Dict[str, Any]]:
        """Get cached state for a mesh infrastructure node."""
        return self.infrastructure_states.get(mesh_addr)
    
    @staticmethod
    def _format_mac(address: int) -> str:
        """Format integer address as MAC string."""
        addr_hex = f"{address:012X}"
        return ':'.join([addr_hex[i:i+2] for i in range(0, 12, 2)])
    
    async def _async_update_data(self) -> Dict[int, Dict[str, Any]]:
        """Fetch data from mesh network via periodic broadcast query."""
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
            )
        except Exception as e:
            _LOGGER.warning("Periodic poll failed: %s - marking disconnected", e)
            self.login_valid = False
            self.connected = False
        
        return self.light_states.copy()
