"""Temporary legacy-app UDP receiver and credential-free import validation."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
import time
from copy import deepcopy

from .discovery import normalize_mac, is_relay_device
from .groups import validate_group_address
from .share_proxy import decode_forwarded_packet, encode_forwarded_packet
from .const import (CONF_DISCOVERED_LIGHT_MESH_ADDRESSES, CONF_ENROLLED_MESH_ADDRESSES,
                    CONF_INFRASTRUCTURE_MESH_ADDRESSES)

APP = 'com.ws.mesh.custom.rgb_cct'
PREFIX = b'msg_send_data-0-'
IMPORT_KEY = 'app_import'
IMPORT_ENROLLMENT_VERSION = 'import_enrollment_version'
LISTENER_KEY = 'btalliance_share_listener'
PENDING_KEY = 'pending_app_import'
REVIEW_TIMEOUT = 24 * 60 * 60


def import_enrollment_options(hub, imported, selected_devices):
    """Grant enrollment only for this explicitly confirmed selection."""
    cached = set(hub.cached_light_mesh_addresses)
    enrolled = set(hub.enrolled_mesh_addresses)
    infrastructure = set(hub.infrastructure_mesh_addresses)
    for raw in selected_devices:
        address = int(raw)
        if is_relay_device(imported['devices'][str(address)]):
            infrastructure.add(address)
            cached.discard(address)
            enrolled.discard(address)
        elif address not in infrastructure and not hub.is_excluded_address(address):
            cached.add(address)
            enrolled.add(address)
    return {
        IMPORT_ENROLLMENT_VERSION: 1,
        CONF_DISCOVERED_LIGHT_MESH_ADDRESSES: ','.join(map(str, sorted(cached))),
        CONF_ENROLLED_MESH_ADDRESSES: ','.join(map(str, sorted(enrolled))),
        CONF_INFRASTRUCTURE_MESH_ADDRESSES: ','.join(map(str, sorted(infrastructure))),
    }


def register_listener(hass, entry, listener):
    """Keep a sanitized review across hub reloads and make receipt visible in HA."""
    from homeassistant.components import persistent_notification

    notification_id = f'btalliance_share_{entry.entry_id}'
    hass.data[LISTENER_KEY] = (entry.entry_id, listener)

    def changed():
        options = dict(entry.options)
        if listener.state == 'received' and listener.staged is not None:
            previous = options.get(PENDING_KEY, {})
            expires = (previous.get('expires_at') if previous.get('data') == listener.staged
                       else time.time() + REVIEW_TIMEOUT)
            options[PENDING_KEY] = {'expires_at': expires, 'data': deepcopy(listener.staged)}
            persistent_notification.async_create(hass,
                f"Received {len(listener.staged['devices'])} devices and {len(listener.staged['groups'])} groups "
                f"for {listener.mesh_name}. **Nothing has been imported yet.** "
                'Open [BTAlliance](/config/integrations/integration/btalliance), choose Configure, '
                'then Import Legacy App Settings to select devices/groups and confirm. '
                'This review is retained for 24 hours; a new phone transfer is not required.',
                title='BTAlliance Settings Ready to Review', notification_id=notification_id)
        elif listener.state in ('stopped', 'timed_out'):
            options.pop(PENDING_KEY, None)
            persistent_notification.async_dismiss(hass, notification_id)
            if hass.data.get(LISTENER_KEY) == (entry.entry_id, listener):
                hass.data.pop(LISTENER_KEY, None)
        else:
            return
        if options != dict(entry.options):
            hass.config_entries.async_update_entry(entry, options=options)

    unsubscribe = listener.add_state_callback(changed)

    def cleanup():
        unsubscribe()
        if hass.data.get(LISTENER_KEY) == (entry.entry_id, listener):
            hass.data.pop(LISTENER_KEY, None)
        listener.stop('unloaded')
        listener.staged = None

    entry.async_on_unload(cleanup)
    if listener.state == 'received':
        changed()


def restore_pending_import(hass, entry, mesh_name):
    """Restore an unconfirmed review without opening a socket or sending commands."""
    if hass.data.get(LISTENER_KEY):
        return None
    pending = entry.options.get(PENDING_KEY)
    if not isinstance(pending, dict):
        return None
    valid = False
    try:
        staged = pending['data']
        remaining = float(pending['expires_at']) - time.time()
        valid = (0 < remaining <= REVIEW_TIMEOUT and staged['mesh_name'] == mesh_name
                 and isinstance(staged['devices'], dict) and isinstance(staged['groups'], dict))
    except (KeyError, TypeError, ValueError):
        pass
    if not valid:
        options = dict(entry.options)
        options.pop(PENDING_KEY, None)
        hass.config_entries.async_update_entry(entry, options=options)
        return None
    listener = ShareSettingsListener(mesh_name, None, None, f'Home Assistant {mesh_name}')
    listener.staged = deepcopy(staged)
    listener.state = 'received'
    register_listener(hass, entry, listener)
    listener._expiry = asyncio.get_running_loop().call_later(remaining, listener.discard)
    return listener


def _integer(value, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError('Invalid address or type')
    return value


def _name(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 80 or any(ord(c) < 32 for c in value):
        raise ValueError('Invalid name')
    return value.strip()


def parse_export(packet: bytes, mesh_name: str) -> dict:
    """Validate an entire export before staging only non-secret fields."""
    if len(packet) > 65507 or not packet.startswith(PREFIX):
        raise ValueError('Invalid settings packet')
    try:
        document = json.loads(packet[len(PREFIX):].decode('utf-8').strip('\x00\r\n '))
        if not isinstance(document, dict) or document.get('property') != APP:
            raise ValueError('Wrong application')
        data = document['data']
        if data['mesh']['login_name'] != mesh_name:
            raise ValueError('Wrong network')
        devices, groups = {}, {}
        if not isinstance(data.get('device', []), list) or not isinstance(data.get('group', []), list):
            raise ValueError('Invalid inventory')
        if len(data.get('device', [])) > 254 or len(data.get('group', [])) > 254:
            raise ValueError('Inventory too large')
        for node in data.get('device', []):
            address = _integer(node['meshId'], 1, 254)
            if str(address) in devices:
                raise ValueError('Duplicate device address')
            device = {'name': _name(node['name']),
                      'device_type': _integer(node['type'], 0, 65535)}
            if node.get('address'):
                device['mac_address'] = normalize_mac(node['address'])
            devices[str(address)] = device
        for group in data.get('group', []):
            address = validate_group_address(_integer(group['meshId'], 32769, 33022))
            if str(address) in groups:
                raise ValueError('Duplicate group address')
            members = json.loads(group.get('dev_array') or '[]')
            if not isinstance(members, list) or len(members) > 254:
                raise ValueError('Invalid group members')
            ids = sorted({_integer(member['_id'], 1, 254) for member in members})
            if any(str(member) not in devices for member in ids):
                raise ValueError('Unknown group member')
            groups[str(address)] = {'name': _name(group['name']), 'members': ids}
        return {'mesh_name': mesh_name, 'devices': devices, 'groups': groups}
    except (KeyError, TypeError, AttributeError, UnicodeError, RecursionError) as err:
        raise ValueError('Invalid settings export') from err


def select_import(staged, device_addresses, group_addresses, hub):
    """Merge selected records, respecting exclusions and known address collisions."""
    previous = hub.entry.options.get(IMPORT_KEY, {})
    if previous.get('mesh_name') != hub.mesh_name:
        previous = {}
    devices = dict(previous.get('devices', {}))
    groups = dict(previous.get('groups', {}))
    if staged['mesh_name'] != hub.mesh_name:
        raise ValueError('Wrong network')
    for raw in device_addresses:
        address = int(raw)
        node = staged['devices'][str(address)]
        observed = hub.get_device_metadata(address)
        if (hub.is_excluded_address(address) or node.get('mac_address') in hub.excluded_mac_addresses
                or observed.get('address_collision')
                or (node.get('mac_address') and observed.get('mac_addresses')
                    and node['mac_address'] not in observed['mac_addresses'])):
            raise ValueError('Excluded or ambiguous device')
        devices[str(address)] = dict(node)
    for raw in group_addresses:
        group = staged['groups'][str(raw)]
        groups[str(raw)] = {'name': group['name'],
                            'members': [member for member in group['members']
                                        if str(member) in devices and not hub.is_excluded_address(member)]}
    return {'mesh_name': hub.mesh_name, 'devices': devices, 'groups': groups}


class ShareSettingsListener(asyncio.DatagramProtocol):
    """One-shot, time-limited receiver; no raw exports or credentials are stored."""

    def __init__(self, mesh_name, sender, broadcast, name, proxy_key=None, *, networks=(), proxy_host=None):
        self.mesh_name = mesh_name
        self.sender = sender
        self.broadcast = broadcast
        self.name = name
        self.proxy_key = proxy_key
        self.proxy_host = proxy_host
        self.networks = tuple(networks)
        self._proxy_nonces = set()
        self.transport = None
        self.staged = None
        self.state = 'listening'
        self.rejected = 0
        self._advertisement = None
        self._expiry = None
        self._state_callbacks = set()

    def add_state_callback(self, callback):
        self._state_callbacks.add(callback)
        return lambda: self._state_callbacks.discard(callback)

    @classmethod
    async def async_start(cls, mesh_name, bind, network, sender, name, duration=300):
        subnet = ipaddress.IPv4Network(network, strict=False)
        local, phone = ipaddress.IPv4Address(bind), ipaddress.IPv4Address(sender)
        if (not subnet.is_private or not local.is_private or not phone.is_private
                or local.is_unspecified or local.is_multicast or phone.is_unspecified or phone.is_multicast
                or local.is_loopback or phone.is_loopback
                or phone not in subnet or local == phone
                or local in (subnet.network_address, subnet.broadcast_address)
                or phone in (subnet.network_address, subnet.broadcast_address)):
            raise ValueError('Use local LAN and phone addresses in the same subnet')
        key_hex = os.environ.get('BTALLIANCE_SHARE_PROXY_KEY', '')
        proxy_key = bytes.fromhex(key_hex) if key_hex else None
        if proxy_key is not None and len(proxy_key) != 32:
            raise ValueError('Invalid forwarding key configuration')
        listener = cls(mesh_name, str(phone), str(subnet.broadcast_address), _name(name), proxy_key)
        return await cls._async_open(listener, str(local), duration)

    @classmethod
    async def async_start_auto(cls, hass, mesh_name, name, duration=300):
        """Use HA's network interfaces, with no address fields in the options flow."""
        from homeassistant.components.network import async_get_adapters

        networks = set()
        for adapter in await async_get_adapters(hass):
            if not adapter.get('enabled', True):
                continue
            for ipv4 in adapter.get('ipv4', []):
                network = ipaddress.IPv4Network(f"{ipv4['address']}/{ipv4['network_prefix']}", strict=False)
                if network.is_private and not network.network_address.is_loopback:
                    networks.add(network)
        key_hex = os.environ.get('BTALLIANCE_SHARE_PROXY_KEY', '')
        key = bytes.fromhex(key_hex) if key_hex else None
        proxy_host = os.environ.get('BTALLIANCE_SHARE_PROXY_HOST') if key else None
        if key is not None and (len(key) != 32 or not proxy_host):
            raise ValueError('Incomplete forwarding configuration')
        if not networks and key is None:
            raise ValueError('No enabled private network interfaces')
        listener = cls(mesh_name, None, None, _name(name), key, networks=networks, proxy_host=proxy_host)
        return await cls._async_open(listener, '0.0.0.0', duration)

    @classmethod
    async def _async_open(cls, listener, bind, duration):
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setblocking(False)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind((bind, 58024))
            await loop.create_datagram_endpoint(lambda: listener, sock=sock)
        except BaseException:
            sock.close()
            raise
        listener._expiry = loop.call_later(duration, listener.stop, 'timed_out')
        return listener

    def connection_made(self, transport):
        self.transport = transport
        self._announce()

    def _announce(self):
        if self.transport is None:
            return
        packet = ('msg_client_online-0-' + self.name).encode()
        self._send(packet)
        self._advertisement = asyncio.get_running_loop().call_later(1, self._announce)

    def _send(self, packet, target=None):
        if self.proxy_host:
            wrapped = encode_forwarded_packet(packet, target or '0.0.0.0', self.proxy_key)
            self.transport.sendto(wrapped, (self.proxy_host, 58024))
        else:
            addresses = {target} if target else ({self.broadcast, self.sender} - {None}) | {
                str(network.broadcast_address) for network in self.networks}
            for address in addresses:
                self.transport.sendto(packet, (address, 58025))

    def _accepts_sender(self, sender, forwarded=False):
        if self.sender is not None:
            return sender == self.sender
        address = ipaddress.IPv4Address(sender)
        if address.is_loopback or address.is_unspecified or address.is_multicast or not address.is_private:
            return False
        return forwarded or any(address in network and address not in (network.network_address, network.broadcast_address)
                                for network in self.networks)

    def datagram_received(self, packet, peer):
        if self.state != 'listening':
            return
        try:
            sender = peer[0]
            if self.proxy_key is not None:
                sender, nonce, packet = decode_forwarded_packet(packet, self.proxy_key)
                if not self._accepts_sender(sender, forwarded=True):
                    return
                if nonce in self._proxy_nonces or len(self._proxy_nonces) >= 256:
                    raise ValueError('Replayed packet or forwarding limit reached')
                self._proxy_nonces.add(nonce)
            elif not self._accepts_sender(sender):
                return
            staged = parse_export(packet, self.mesh_name)
        except ValueError:
            self.rejected += 1
            return
        self.staged = staged
        self._send(b'msg_ok', sender)
        self.stop('received')
        self._expiry = asyncio.get_running_loop().call_later(REVIEW_TIMEOUT, self.discard)

    def error_received(self, exc):
        # Unreachable UDP announcements do not expose payloads or end the window.
        pass

    def stop(self, state='stopped'):
        for handle in (self._advertisement, self._expiry):
            if handle:
                handle.cancel()
        if self.transport:
            self._send(b'msg_client_offline-0-')
            self.transport.close()
            self.transport = None
        self.state = state
        for callback in tuple(self._state_callbacks):
            callback()

    def discard(self):
        self.staged = None
        self.stop()
