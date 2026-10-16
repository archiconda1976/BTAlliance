"""Protocol layer for Telink BLE Mesh command/response handling."""

import datetime
import logging
import hmac
import re
from typing import Optional

from .const import (
    COMMAND_ON_OFF, COMMAND_SET_LUMINANCE, COMMAND_SET_RGB_LUM,
    COMMAND_SET_CT_LUM, COMMAND_QUERY_STATUS, COMMAND_SET_DATETIME,
    NOTIFY_STATUS_RESPONSE, NOTIFY_LIGHT_STATUS,
    MODE_RGB, MODE_COLOR_TEMP, VENDOR_ID, BROADCAST_ADDRESS
)
from .const import COMMAND_QUERY_FIRMWARE, NOTIFY_FIRMWARE, DEFAULT_LONG_TERM_KEY
from .const import COMMAND_GROUP_MEMBERSHIP, COMMAND_QUERY_GROUPS, NOTIFY_GROUPS
from .groups import validate_group_address
from .discovery import validate_network_name, validate_password
from .crypto import TelinkCrypto

_LOGGER = logging.getLogger(__name__)


class TelinkProtocol:
    """Handles Telink BLE Mesh protocol packet construction and parsing."""
    
    def __init__(self, mac_bytes: bytes, mesh_name: str, password: str):
        """Initialize protocol handler."""
        self.mac_bytes = mac_bytes
        self.mesh_name_bytes = TelinkCrypto.pad_string(mesh_name, 16)
        self.mesh_password_bytes = TelinkCrypto.pad_string(password, 16)
        self.session_key: Optional[bytearray] = None
        self.sequence_number = 1
        self.target_address = (0x0001).to_bytes(2, "little")
    
    def set_target_address(self, mesh_addr: int) -> None:
        """Set target mesh address for commands."""
        self.target_address = mesh_addr.to_bytes(2, "little")
    
    def get_target_address(self) -> int:
        """Get current target mesh address."""
        return int.from_bytes(self.target_address, "little")
    
    def _next_sequence(self) -> int:
        """Increment and return sequence number."""
        self.sequence_number += 1
        if self.sequence_number == 0:
            self.sequence_number = 1
        return self.sequence_number
    
    def _build_command_frame(self, opcode: int) -> bytearray:
        """Build base command frame."""
        seq = self._next_sequence()
        seq_bytes = seq.to_bytes(3, "big")
        
        data = bytearray(20)
        data[0:3] = seq_bytes
        data[5] = self.target_address[0]
        data[6] = self.target_address[1]
        data[7] = opcode
        data[8] = (VENDOR_ID >> 8) & 0xFF
        data[9] = VENDOR_ID & 0xFF
        return data
    
    def _encrypt_command(self, data: bytearray) -> bytearray:
        """Encrypt command data for transmission."""
        if self.session_key is None:
            raise RuntimeError("Session key not established")
        nonce = TelinkCrypto.get_nonce_ivm(self.mac_bytes, self.sequence_number)
        return TelinkCrypto.encrypt_command(self.session_key, nonce, data)
    
    def generate_login_payload(self, session_random: bytes) -> bytearray:
        """Generate encrypted login payload."""
        plaintext = bytearray(16)
        for i in range(16):
            plaintext[i] = self.mesh_name_bytes[i] ^ self.mesh_password_bytes[i]
        
        sk = bytearray(16)
        sk[:8] = session_random
        
        encrypted = TelinkCrypto.encrypt(sk, bytes(plaintext))
        
        payload = bytearray(16)
        payload[0:8] = session_random
        payload[8:16] = encrypted[8:16][::-1]
        return payload
    
    def process_login_response(self, response: bytes, session_random: bytes) -> bool:
        """Process login response and derive session key."""
        if len(response) < 17 or response[0] != 0x0D:
            return False
        
        sk = bytearray(response[1:17])
        rands = bytearray(response[1:9])
        
        self.session_key = TelinkCrypto.derive_session_key(
            self.mesh_name_bytes,
            self.mesh_password_bytes,
            session_random,
            rands,
            sk
        )
        return self.session_key is not None
    
    def generate_on_off_command(self, state: bool) -> bytearray:
        """Generate ON/OFF command."""
        data = self._build_command_frame(COMMAND_ON_OFF)
        data[10] = 0x01 if state else 0x00
        return self._encrypt_command(data)

    def generate_firmware_query(self) -> bytearray:
        """Request the firmware identifier using the app's C7/10 query."""
        data = self._build_command_frame(COMMAND_QUERY_FIRMWARE)
        data[10:12] = bytes([0x10, 0x00])
        return self._encrypt_command(data)

    def generate_group_query(self) -> bytearray:
        """Request short group memberships using the app's DD/08/01 query."""
        data = self._build_command_frame(COMMAND_QUERY_GROUPS)
        data[10:12] = b'\x08\x01'
        return self._encrypt_command(data)

    def generate_group_membership(self, group_address: int, add: bool) -> bytearray:
        """Assign/remove a group on the target individual mesh device."""
        group_address = validate_group_address(group_address)
        if not 1 <= self.get_target_address() <= 254:
            raise ValueError("Membership changes require an individual device address")
        data = self._build_command_frame(COMMAND_GROUP_MEMBERSHIP)
        data[10:13] = bytes([int(add), group_address & 0xFF, group_address >> 8])
        return self._encrypt_command(data)

    def generate_network_packets(self, name: str, password: str) -> list[bytes]:
        """Pairing-characteristic writes; reuse the established AES primitives."""
        validate_network_name(name)
        validate_password(password)
        if self.session_key is None:
            raise RuntimeError("Session key not established")
        values = [TelinkCrypto.pad_string(name, 16), TelinkCrypto.pad_string(password, 16), DEFAULT_LONG_TERM_KEY]
        return [bytes([opcode]) + bytes(TelinkCrypto.aes_att_encryption(self.session_key, value))
                for opcode, value in zip((0x04, 0x05, 0x06), values, strict=True)]

    def validate_network_confirmation(self, response: bytes, name: str, password: str) -> bool:
        """Verify the app's encrypted name/password/LTK confirmation."""
        if self.session_key is None or len(response) != 17 or response[0] != 0x07:
            return False
        name_bytes = TelinkCrypto.pad_string(name, 16)
        password_bytes = TelinkCrypto.pad_string(password, 16)
        plaintext = bytes(a ^ b ^ c for a, b, c in zip(name_bytes, password_bytes, DEFAULT_LONG_TERM_KEY, strict=True))
        expected = TelinkCrypto.aes_att_encryption(self.session_key, plaintext)
        return hmac.compare_digest(response[1:], bytes(expected))

    def generate_factory_reset_packet(self, random: bytes) -> bytes:
        """Direct pairing delete, never a routed mesh reset or broadcast."""
        if len(random) != 8:
            raise ValueError("Pairing random must contain eight bytes")
        return bytes([0x0A]) + bytes(self.generate_login_payload(random))
    
    def generate_luminance_command(self, level: int) -> bytearray:
        """Generate luminance command (0-100)."""
        data = self._build_command_frame(COMMAND_SET_LUMINANCE)
        data[10] = max(0, min(100, level))
        return self._encrypt_command(data)
    
    def generate_rgb_command(self, red: int, green: int, blue: int) -> bytearray:
        """Generate RGB color command."""
        data = self._build_command_frame(COMMAND_SET_RGB_LUM)
        data[10] = MODE_RGB
        data[11] = max(0, min(255, red))
        data[12] = max(0, min(255, green))
        data[13] = max(0, min(255, blue))
        return self._encrypt_command(data)
    
    def generate_color_temp_command(self, color_temp: int) -> bytearray:
        """Generate color temperature command (0=warm, 100=cool)."""
        data = self._build_command_frame(COMMAND_SET_CT_LUM)
        data[10] = MODE_COLOR_TEMP
        data[11] = max(0, min(100, color_temp))
        return self._encrypt_command(data)
    
    def generate_query_status_command(self) -> bytearray:
        """Generate status query command."""
        data = self._build_command_frame(COMMAND_QUERY_STATUS)
        data[10] = 0x01
        return self._encrypt_command(data)
    
    def generate_datetime_command(self) -> bytearray:
        """Generate set datetime command (broadcast)."""
        original_addr = self.target_address
        self.target_address = BROADCAST_ADDRESS.to_bytes(2, "little")
        
        now = datetime.datetime.now()
        data = self._build_command_frame(COMMAND_SET_DATETIME)
        data[10] = now.year & 0xFF
        data[11] = (now.year >> 8) & 0xFF
        data[12] = now.month
        data[13] = now.day
        data[14] = now.hour
        data[15] = now.minute
        data[16] = now.second
        
        result = self._encrypt_command(data)
        self.target_address = original_addr
        return result
    
    def parse_notification(self, data: bytearray) -> Optional[dict]:
        """Parse and decrypt notification data."""
        if self.session_key is None or len(data) != 20:
            return None
        
        nonce = TelinkCrypto.get_nonce_ivs(self.mac_bytes)
        nonce[3:8] = data[0:5]
        
        try:
            decrypted = TelinkCrypto.decrypt_response(self.session_key, nonce, data)
        except Exception:
            return None
        
        if decrypted is None or int.from_bytes(decrypted[8:10], "big") != VENDOR_ID:
            return None
        
        opcode = decrypted[7]
        result = {
            'opcode': opcode,
            'source': int.from_bytes(decrypted[3:5], "little"),
            'mic': bytes(decrypted[5:7]).hex(),
            'raw': decrypted,
        }
        
        if opcode == NOTIFY_GROUPS:
            groups = []
            terminated = False
            for group_id in decrypted[10:20]:
                if group_id == 0xFF:
                    terminated = True
                    break
                if group_id:
                    groups.append(0x8000 | group_id)
            result['group_addresses'] = sorted(set(groups))
            result['groups_complete'] = terminated

        elif opcode == NOTIFY_STATUS_RESPONSE:
            result['red'] = decrypted[10]
            result['green'] = decrypted[11]
            result['blue'] = decrypted[12]
            result['color_temp'] = decrypted[13]
            result['warm'] = decrypted[14]
            result['cool'] = decrypted[15]
            result['luminance'] = decrypted[16]
            result['is_on'] = result['luminance'] > 0 or result['warm'] > 0 or result['cool'] > 0
            
        elif opcode == NOTIFY_LIGHT_STATUS:
            records = []
            for offset in (10, 14):
                address, counter, luminance, auxiliary = decrypted[offset:offset + 4]
                if address == 0 or (address == 255 and luminance == 255):
                    break
                if not 1 <= address <= 254:
                    continue
                records.append({
                    'light_addr': address, 'status_counter': counter,
                    'online': counter != 0, 'is_on': counter != 0 and luminance > 0,
                    'luminance': luminance, 'auxiliary': auxiliary,
                })
            result['records'] = records
            if records:
                result.update(records[0])

        elif opcode == NOTIFY_FIRMWARE:
            name = bytes(decrypted[14:20]).rstrip(b"\x00").decode("ascii", errors="ignore")
            if re.fullmatch(r"[A-Za-z0-9]{1,6}", name):
                result['firmware_name'] = name
        
        return result
