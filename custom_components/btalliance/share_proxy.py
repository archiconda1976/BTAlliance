"""Authenticated local UDP forwarding, separate from Telink mesh encryption."""

import hashlib
import hmac
import ipaddress
import secrets
import time

PREFIX = b'BTALLIANCE_PROXY_1\x00'
HEADER_SIZE = len(PREFIX) + 8 + 16 + 4
MAX_DATAGRAM = 65507


def encode_forwarded_packet(packet, sender, key, *, now=None, nonce=None):
    """Preserve the original LAN sender across Docker Desktop source NAT."""
    if len(key) != 32 or len(packet) + HEADER_SIZE + 32 > MAX_DATAGRAM:
        raise ValueError('Invalid forwarding key or packet size')
    nonce = secrets.token_bytes(16) if nonce is None else nonce
    if len(nonce) != 16:
        raise ValueError('Invalid nonce')
    header = (PREFIX + int(time.time() if now is None else now).to_bytes(8, 'big')
              + nonce + ipaddress.IPv4Address(sender).packed)
    signature = hmac.new(key, header + packet, hashlib.sha256).digest()
    return header + signature + packet


def decode_forwarded_packet(packet, key, *, now=None):
    """Authenticate sender and payload; callers enforce per-listener replay checks."""
    if (len(key) != 32 or len(packet) < HEADER_SIZE + 32 or len(packet) > MAX_DATAGRAM
            or not packet.startswith(PREFIX)):
        raise ValueError('Invalid forwarded packet')
    header = packet[:HEADER_SIZE]
    payload = packet[HEADER_SIZE + 32:]
    expected = hmac.new(key, header + payload, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, packet[HEADER_SIZE:HEADER_SIZE + 32]):
        raise ValueError('Forwarding authentication failed')
    timestamp = int.from_bytes(header[len(PREFIX):len(PREFIX) + 8], 'big')
    if abs((time.time() if now is None else now) - timestamp) > 30:
        raise ValueError('Expired forwarded packet')
    return str(ipaddress.IPv4Address(header[-4:])), header[-20:-4], payload
