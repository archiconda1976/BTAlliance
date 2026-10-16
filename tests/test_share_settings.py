"""Legacy exports remain untrusted, credential-free, and explicitly selected."""

import asyncio
import json
import socket
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.btalliance.share_settings import (
    APP, PREFIX, IMPORT_KEY, ShareSettingsListener, parse_export, select_import,
    PENDING_KEY, LISTENER_KEY, register_listener, restore_pending_import,
    import_enrollment_options,
)
from custom_components.btalliance.coordinator import BTAllianceMeshCoordinator
from custom_components.btalliance.config_flow import BTAllianceOptionsFlow
from custom_components.btalliance.light import BTAllianceMeshLight
from custom_components.btalliance.binary_sensor import BTAllianceMeshInfrastructureNode
from custom_components.btalliance.share_proxy import encode_forwarded_packet, decode_forwarded_packet


@pytest.fixture
def document():
    return {'property': APP, 'data': {
        'mesh': {'login_name': 'Fulife', 'login_psd': 'do-not-store', 'program_data': 'private'},
        'device': [{'meshId': 3, 'name': 'Path-0-Light', 'type': 40968},
                   {'meshId': 55, 'name': 'Garden Relay', 'type': 40979,
                    'address': 'FF:05:01:0B:A4:37'}],
        'group': [{'meshId': 32785, 'name': 'Group1', 'dev_array': '[{"_id":3}]'}]}}


def packet(document):
    return PREFIX + json.dumps(document).encode()


def test_parse_export_strips_all_credentials_and_preserves_names(document):
    result = parse_export(packet(document), 'Fulife')
    assert result['devices']['3']['name'] == 'Path-0-Light'
    assert result['groups']['32785']['members'] == [3]
    assert result['devices']['55']['mac_address'] == 'FF:05:01:0B:A4:37'
    assert 'do-not-store' not in json.dumps(result)
    assert 'private' not in json.dumps(result)


@pytest.mark.parametrize('mutation', [
    lambda d: d.update(property='another.app'),
    lambda d: d['data']['mesh'].update(login_name='Kelly'),
    lambda d: d['data']['device'][0].update(meshId=255),
    lambda d: d['data']['device'][0].update(meshId=True),
    lambda d: d['data']['device'][0].update(type='40968'),
    lambda d: d['data']['device'][0].update(name='bad\x00name'),
    lambda d: d['data']['device'].append(d['data']['device'][0]),
    lambda d: d['data']['device'][1].update(address='invalid'),
    lambda d: d['data']['group'][0].update(meshId=65535),
    lambda d: d['data']['group'][0].update(dev_array='[{"_id":91}]'),
    lambda d: d['data']['group'][0].update(dev_array='not-json'),
    lambda d: d['data'].update(device={}),
])
def test_invalid_export_rejected_atomically(document, mutation):
    mutation(document)
    with pytest.raises(ValueError):
        parse_export(packet(document), 'Fulife')


@pytest.mark.parametrize('data', [b'hello', PREFIX + b'[]', PREFIX + b'null',
                                      PREFIX + b'\xff', PREFIX + b'{' * 2000,
                                      PREFIX + b' ' * 65507], ids=['prefix', 'array', 'null', 'unicode', 'nesting', 'oversize'])
def test_malformed_datagrams(data):
    with pytest.raises(ValueError):
        parse_export(data, 'Fulife')


def import_hub():
    return SimpleNamespace(mesh_name='Fulife', entry=SimpleNamespace(options={}),
        cached_light_mesh_addresses=set(), enrolled_mesh_addresses=set(), infrastructure_mesh_addresses=set(),
        excluded_mac_addresses=set(), is_excluded_address=lambda _: False,
        get_device_metadata=lambda _: {})


def test_selection_is_incremental_and_filters_group_members(document):
    staged = parse_export(packet(document), 'Fulife')
    hub = import_hub()
    result = select_import(staged, [], ['32785'], hub)
    assert not result['devices']
    assert result['groups']['32785']['members'] == []
    hub.entry.options[IMPORT_KEY] = select_import(staged, ['3'], [], hub)
    result = select_import(staged, ['55'], ['32785'], hub)
    assert set(result['devices']) == {'3', '55'}
    assert result['groups']['32785']['members'] == [3]


@pytest.mark.parametrize('reason', ['excluded', 'collision', 'mac', 'mac_conflict', 'network'])
def test_selection_rejects_exclusions_and_conflicting_identity(document, reason):
    staged = parse_export(packet(document), 'Fulife')
    hub = import_hub()
    if reason == 'excluded':
        hub.is_excluded_address = lambda _: True
    elif reason == 'collision':
        hub.get_device_metadata = lambda _: {'address_collision': True}
    elif reason == 'mac':
        hub.excluded_mac_addresses.add('FF:05:01:0B:A4:37')
    elif reason == 'mac_conflict':
        hub.get_device_metadata = lambda _: {'mac_addresses': ['FF:00:00:00:00:01']}
    else:
        staged['mesh_name'] = 'Kelly'
    with pytest.raises(ValueError):
        select_import(staged, ['55'], [], hub)


@pytest.mark.asyncio
async def test_listener_sender_filter_ack_and_one_shot(document):
    listener = ShareSettingsListener('Fulife', '192.168.1.21', '192.168.1.255', 'Home Assistant')
    transport = Mock()
    listener.connection_made(transport)
    listener._expiry = asyncio.get_running_loop().call_later(300, listener.stop)
    listener.datagram_received(packet(document), ('192.168.1.22', 50000))
    assert listener.staged is None
    listener.datagram_received(b'invalid', ('192.168.1.21', 50000))
    assert listener.rejected == 1
    listener.datagram_received(packet(document), ('192.168.1.21', 50000))
    assert listener.state == 'received' and listener.staged is not None
    transport.sendto.assert_any_call(b'msg_ok', ('192.168.1.21', 58025))
    transport.close.assert_called_once()
    listener.datagram_received(b'invalid', ('192.168.1.21', 50000))
    assert listener.rejected == 1
    listener.discard()
    assert listener.staged is None and listener._expiry.cancelled()


@pytest.mark.asyncio
@pytest.mark.parametrize('bind,network,phone', [
    ('0.0.0.0', '192.168.1.0/24', '192.168.1.20'),
    ('192.168.1.2', '192.168.1.0/24', '192.168.2.20'),
    ('127.0.0.1', '127.0.0.0/8', '127.0.0.2'),
    ('192.168.1.2', '0.0.0.0/0', '192.168.1.20'),
    ('192.168.1.2', '192.168.1.0/24', '192.168.1.2'),
    ('192.168.1.2', '192.168.1.0/24', '192.168.1.255'),
])
async def test_invalid_receiver_configuration_never_binds(bind, network, phone):
    with pytest.raises(ValueError):
        await ShareSettingsListener.async_start('Fulife', bind, network, phone, 'Home Assistant')


@pytest.mark.asyncio
async def test_import_initialization_separates_live_metadata_and_group_provenance(document, tmp_path):
    imported = parse_export(packet(document), 'Fulife')
    hass = HomeAssistant(str(tmp_path))
    options = {IMPORT_KEY: imported, **import_enrollment_options(import_hub(), imported, ['3', '55'])}
    entry = SimpleNamespace(options=options, data={}, entry_id='test', async_on_unload=Mock())
    hub = BTAllianceMeshCoordinator(hass, entry, 1, 'Fulife', '2846',
        cached_light_mesh_addresses={3}, infrastructure_mesh_addresses={55})
    assert hub.cached_light_mesh_addresses == {3}
    assert hub.infrastructure_mesh_addresses == {55}
    assert hub.light_groups[3] == {32785}
    assert hub.get_group_metadata(32785)['state_basis'] == 'app_import'
    meta = hub.get_device_metadata(3)
    assert meta['device_type'] is None and meta['mac_address'] is None
    assert meta['app_import_name'] == 'Path-0-Light'
    assert meta['group_membership_verified_at'] is None
    assert not hub.connected and not hub.login_valid
    light = BTAllianceMeshLight(hub, 3, 'Fulife', 'test', None)
    relay = BTAllianceMeshInfrastructureNode(hub, 55, 'Fulife', 'test', None)
    assert light.name is None and light.device_info['name'] == 'Path-0-Light'
    assert relay.name is None and relay.device_info['name'] == 'Garden Relay'


@pytest.mark.asyncio
async def test_live_group_membership_wins_over_app(document, tmp_path):
    hass = HomeAssistant(str(tmp_path))
    imported = parse_export(packet(document), 'Fulife')
    entry = SimpleNamespace(options={IMPORT_KEY: imported, 'mesh_groups': {
        'mesh_name': 'Fulife', 'groups': {'32785': {'name': 'Renamed in HA'}},
        'memberships': {'3': {'groups': [32786], 'verified_at': 123}}}}, data={}, entry_id='test', async_on_unload=Mock())
    hub = BTAllianceMeshCoordinator(hass, entry, 1, 'Fulife', '2846')
    assert hub.light_groups[3] == {32786}
    assert hub.group_membership_verified[3] == 123
    assert hub.mesh_groups[32785]['name'] == 'Renamed in HA'


@pytest.mark.asyncio
async def test_options_preview_confirmation_and_stop(document, tmp_path, monkeypatch):
    hass = HomeAssistant(str(tmp_path))
    imported = parse_export(packet(document), 'Fulife')
    hub = import_hub()
    hub.cached_light_mesh_addresses = {3}
    hub.entry.entry_id = 'test'
    hub.entry.async_on_unload = Mock()
    hass.data['btalliance'] = {'test': hub}
    flow = BTAllianceOptionsFlow()
    flow.hass = hass
    monkeypatch.setattr(type(flow), 'config_entry', property(lambda _: hub.entry))
    listener = ShareSettingsListener('Fulife', '192.168.1.20', '192.168.1.255', 'HA')
    listener.staged = imported
    listener.state = 'received'
    hass.data['btalliance_share_listener'] = ('test', listener)
    form = await flow.async_step_share_wait()
    assert form['step_id'] == 'share_preview'
    result = await flow.async_step_share_preview({'devices': ['3'], 'groups': ['32785'], 'confirm': False})
    assert result['errors']['base'] == 'share_invalid_selection'
    result = await flow.async_step_share_preview({'devices': ['3'], 'groups': ['32785'], 'confirm': True})
    assert result['data'][IMPORT_KEY]['devices'] == {'3': imported['devices']['3']}
    assert result['data']['enrolled_mesh_addresses'] == '3'
    assert result['data']['discovered_light_mesh_addresses'] == '3'
    assert result['data']['import_enrollment_version'] == 1
    assert 'btalliance_share_listener' not in hass.data
    assert listener.staged is None


@pytest.mark.asyncio
async def test_actual_endpoint_initialization_and_timeout(monkeypatch):
    loop = asyncio.get_running_loop()
    transport = Mock()
    sock = Mock()
    monkeypatch.setattr('custom_components.btalliance.share_settings.socket', SimpleNamespace(
        socket=lambda *_: sock, AF_INET=socket.AF_INET, SOCK_DGRAM=socket.SOCK_DGRAM,
        SOL_SOCKET=socket.SOL_SOCKET, SO_BROADCAST=socket.SO_BROADCAST))

    async def create_endpoint(factory, **kwargs):
        assert kwargs['sock'] is sock
        instance = factory()
        instance.connection_made(transport)
        return transport, instance

    monkeypatch.setattr(loop, 'create_datagram_endpoint', create_endpoint)
    listener = await ShareSettingsListener.async_start('Fulife', '172.17.0.2',
        '192.168.1.0/24', '192.168.1.20', 'HA', duration=0.01)
    sock.bind.assert_called_once_with(('172.17.0.2', 58024))
    await asyncio.sleep(0.03)
    assert listener.state == 'timed_out' and listener.transport is None
    transport.close.assert_called_once()


@pytest.mark.asyncio
async def test_bind_failure_closes_socket(monkeypatch):
    sock = Mock()
    sock.bind.side_effect = OSError('port busy')
    monkeypatch.setattr('custom_components.btalliance.share_settings.socket', SimpleNamespace(
        socket=lambda *_: sock, AF_INET=socket.AF_INET, SOCK_DGRAM=socket.SOCK_DGRAM,
        SOL_SOCKET=socket.SOL_SOCKET, SO_BROADCAST=socket.SO_BROADCAST))
    with pytest.raises(OSError):
        await ShareSettingsListener.async_start('Fulife', '192.168.1.2',
            '192.168.1.0/24', '192.168.1.20', 'HA')
    sock.close.assert_called_once()


@pytest.mark.asyncio
async def test_import_is_network_scoped_and_respects_excluded_mac(document, tmp_path):
    hass = HomeAssistant(str(tmp_path))
    imported = parse_export(packet(document), 'Fulife')
    entry = SimpleNamespace(options={IMPORT_KEY: imported}, data={}, entry_id='test', async_on_unload=Mock())
    hub = BTAllianceMeshCoordinator(hass, entry, 1, 'Kelly', '2846')
    assert not hub.imported_devices and not hub.mesh_groups and not hub.cached_light_mesh_addresses
    hub = BTAllianceMeshCoordinator(hass, entry, 1, 'Fulife', '2846',
        cached_light_mesh_addresses={55}, excluded_mac_addresses={'FF:05:01:0B:A4:37'})
    assert not hub.infrastructure_mesh_addresses and hub.is_excluded_address(55)
    assert 55 not in hub.cached_light_mesh_addresses and 55 not in hub.enrolled_mesh_addresses


@pytest.mark.asyncio
async def test_options_listener_start_stop_and_unload(tmp_path, monkeypatch):
    hass = HomeAssistant(str(tmp_path))
    entry = SimpleNamespace(options={}, data={'mesh_name': 'Fulife'}, entry_id='test', async_on_unload=Mock())
    hass.data['btalliance'] = {'test': SimpleNamespace(mesh_name='Fulife')}
    flow = BTAllianceOptionsFlow()
    flow.hass = hass
    monkeypatch.setattr(type(flow), 'config_entry', property(lambda _: entry))
    listener = ShareSettingsListener('Fulife', '192.168.1.20', '192.168.1.255', 'HA')

    async def start(*_):
        return listener

    monkeypatch.setattr(ShareSettingsListener, 'async_start_auto', start)
    form = await flow.async_step_share_settings()
    assert not form['data_schema'].schema
    result = await flow.async_step_share_settings({})
    assert result['step_id'] == 'share_wait'
    assert hass.data['btalliance_share_listener'][1] is listener
    entry.async_on_unload.call_args[0][0]()
    assert listener.state == 'unloaded' and 'btalliance_share_listener' not in hass.data
    hass.data['btalliance_share_listener'] = ('test', listener)
    result = await flow.async_step_share_wait({'stop': True})
    assert result['type'] == 'create_entry' and 'btalliance_share_listener' not in hass.data


def test_authenticated_forwarding_preserves_sender_and_rejects_tampering():
    key = bytes(range(32))
    wrapped = encode_forwarded_packet(b'payload', '192.168.1.20', key, now=100, nonce=bytes(16))
    assert decode_forwarded_packet(wrapped, key, now=110) == ('192.168.1.20', bytes(16), b'payload')
    for value, candidate, now in ((wrapped[:-1] + b'X', key, 100),
                                  (wrapped, bytes(32), 100), (wrapped, key, 131),
                                  (b'payload', key, 100)):
        with pytest.raises(ValueError):
            decode_forwarded_packet(value, candidate, now=now)
    with pytest.raises(ValueError):
        encode_forwarded_packet(b'x' * 65507, '192.168.1.20', key)


@pytest.mark.asyncio
async def test_nat_forwarded_export_still_requires_phone_key_and_fresh_nonce(document):
    key = bytes(range(32))
    listener = ShareSettingsListener('Fulife', '192.168.1.20', '192.168.1.255', 'HA', key)
    listener.transport = Mock()
    listener.datagram_received(packet(document), ('192.168.1.20', 12345))
    assert listener.staged is None and listener.rejected == 1
    listener.datagram_received(encode_forwarded_packet(packet(document), '192.168.1.21', key), ('172.17.0.1', 12345))
    assert listener.staged is None and not listener._proxy_nonces
    invalid = encode_forwarded_packet(b'invalid', '192.168.1.20', key)
    listener.datagram_received(invalid, ('172.17.0.1', 12345))
    listener.datagram_received(invalid, ('172.17.0.1', 12345))
    assert listener.rejected == 3
    listener.datagram_received(encode_forwarded_packet(packet(document), '192.168.1.20', key), ('172.17.0.1', 12345))
    assert listener.state == 'received'
    listener.discard()


@pytest.mark.asyncio
async def test_auto_listener_infers_enabled_interfaces_without_phone_ip(tmp_path, monkeypatch):
    import ipaddress
    from unittest.mock import AsyncMock
    from homeassistant.components import network

    monkeypatch.delenv('BTALLIANCE_SHARE_PROXY_KEY', raising=False)
    monkeypatch.setattr(network, 'async_get_adapters', AsyncMock(return_value=[
        {'enabled': True, 'ipv4': [{'address': '192.168.1.206', 'network_prefix': 24}]},
        {'enabled': False, 'ipv4': [{'address': '10.0.0.2', 'network_prefix': 24}]},
        {'enabled': True, 'ipv4': [{'address': '127.0.0.1', 'network_prefix': 8}]},
    ]))
    opened = []

    async def open_listener(listener, bind, duration):
        opened.append((bind, duration))
        return listener

    monkeypatch.setattr(ShareSettingsListener, '_async_open', open_listener)
    listener = await ShareSettingsListener.async_start_auto(HomeAssistant(str(tmp_path)), 'Fulife', 'HA')
    assert opened == [('0.0.0.0', 300)]
    assert listener.networks == (ipaddress.ip_network('192.168.1.0/24'),)
    assert listener._accepts_sender('192.168.1.20') and listener._accepts_sender('192.168.1.30')
    assert not listener._accepts_sender('10.0.0.20') and not listener._accepts_sender('8.8.8.8')


@pytest.mark.asyncio
async def test_auto_proxy_listener_routes_announcements_and_ack_without_phone_ip(document):
    key = bytes(range(32))
    listener = ShareSettingsListener('Fulife', None, None, 'HA', key, proxy_host='192.168.1.206')
    transport = Mock()
    listener.connection_made(transport)
    control, target = transport.sendto.call_args[0]
    assert target == ('192.168.1.206', 58024)
    assert decode_forwarded_packet(control, key)[2] == b'msg_client_online-0-HA'
    listener.datagram_received(encode_forwarded_packet(packet(document), '192.168.1.45', key), ('172.17.0.1', 12345))
    controls = [decode_forwarded_packet(call.args[0], key) for call in transport.sendto.call_args_list]
    assert any(sender == '192.168.1.45' and data == b'msg_ok' for sender, _, data in controls)
    assert listener.state == 'received'
    listener.discard()


def test_listener_state_callbacks_can_be_removed():
    listener = ShareSettingsListener('Fulife', None, None, 'HA')
    changed = Mock()
    remove = listener.add_state_callback(changed)
    listener.stop('timed_out')
    changed.assert_called_once()
    remove()
    listener.discard()
    changed.assert_called_once()


@pytest.mark.asyncio
async def test_received_review_survives_reload_without_credentials(document, tmp_path, monkeypatch):
    from homeassistant.components import persistent_notification

    hass = HomeAssistant(str(tmp_path))
    entry = SimpleNamespace(options={}, data={'mesh_name': 'Fulife'}, entry_id='test', async_on_unload=Mock())
    hass.config_entries = SimpleNamespace(async_update_entry=lambda target, **kw: setattr(target, 'options', kw['options']))
    notification = Mock()
    monkeypatch.setattr(persistent_notification, 'async_create', notification)
    monkeypatch.setattr(persistent_notification, 'async_dismiss', Mock())
    listener = ShareSettingsListener('Fulife', '192.168.1.20', '192.168.1.255', 'HA')
    register_listener(hass, entry, listener)
    listener.transport = Mock()
    listener.datagram_received(packet(document), ('192.168.1.20', 58025))
    assert entry.options[PENDING_KEY]['data']['devices']['3']['name'] == 'Path-0-Light'
    assert 'do-not-store' not in json.dumps(entry.options)
    assert 'Nothing has been imported yet' in notification.call_args.args[1]
    expires = entry.options[PENDING_KEY]['expires_at']
    assert not entry.options.get(IMPORT_KEY)
    entry.async_on_unload.call_args.args[0]()
    assert PENDING_KEY in entry.options and LISTENER_KEY not in hass.data
    restored = restore_pending_import(hass, entry, 'Fulife')
    assert restored.staged['groups']['32785']['name'] == 'Group1'
    assert restored.transport is None and restored.state == 'received'
    assert entry.options[PENDING_KEY]['expires_at'] == expires
    restored.discard()
    assert PENDING_KEY not in entry.options and LISTENER_KEY not in hass.data


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['expired', 'foreign_network', 'corrupt'])
async def test_invalid_pending_reviews_are_purged(case, document, tmp_path, monkeypatch):
    hass = HomeAssistant(str(tmp_path))
    pending = {'data': parse_export(packet(document), 'Fulife'), 'expires_at': time.time() + 100}
    if case == 'expired':
        pending['expires_at'] = time.time() - 1
    elif case == 'foreign_network':
        pending['data']['mesh_name'] = 'Kelly'
    else:
        pending['data'] = None
    entry = SimpleNamespace(options={PENDING_KEY: pending}, entry_id='test')
    hass.config_entries = SimpleNamespace(async_update_entry=lambda target, **kw: setattr(target, 'options', kw['options']))
    assert restore_pending_import(hass, entry, 'Fulife') is None
    assert PENDING_KEY not in entry.options


@pytest.mark.asyncio
@pytest.mark.parametrize('custom_entity_name', [None, 'My custom HA name'])
async def test_imported_primary_entity_uses_ha_device_name_once(custom_entity_name, tmp_path, monkeypatch):
    from homeassistant.helpers import device_registry as dr, entity_registry as er

    hass = HomeAssistant(str(tmp_path))
    device = SimpleNamespace(name='garage1', name_by_user=None, area_id=None)
    monkeypatch.setattr(dr, 'async_get', lambda _: SimpleNamespace(async_get=lambda _: device))
    registry_entry = SimpleNamespace(original_name=None, original_name_unprefixed=None,
        name=custom_entity_name, has_entity_name=True, device_id='device', area_id=None, next_name_part=None)
    formatter = getattr(er, 'async_get_legacy_friendly_name', er.async_get_full_entity_name)
    assert formatter(hass, registry_entry) == (custom_entity_name or 'garage1')
