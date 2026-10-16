"""Native group packets, confirmation, state aggregation, and address isolation."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.btalliance.coordinator import BTAllianceMeshCoordinator
from custom_components.btalliance.groups import aggregate_group_state, validate_group_address
from custom_components.btalliance.light import BTAllianceGroupLight
from custom_components.btalliance.protocol import TelinkProtocol, TelinkCrypto


@pytest.fixture
def hub():
    hub = object.__new__(BTAllianceMeshCoordinator)
    hub.mesh_name = 'Fulife'
    hub.mesh_groups = {}
    hub.light_groups = {}
    hub.group_membership_verified = {}
    hub.enrolled_mesh_addresses = {3, 16}
    hub.excluded_mesh_addresses = set()
    hub.excluded_mac_addresses = set()
    hub.infrastructure_mesh_addresses = {55}
    hub.device_metadata = {}
    hub.firmware_names = {}
    hub.light_states = {}
    hub._group_reply_pending = {}
    hub._group_membership_lock = asyncio.Lock()
    hub._new_group_callback = Mock()
    hub._notify_state_change = Mock()
    hub._persist_groups = Mock()
    hub._async_send_command = AsyncMock(return_value=True)
    hub.protocol = TelinkProtocol(bytes(6), 'Fulife', '2846')
    return hub


@pytest.mark.parametrize('address', [0, 3, 0x8000, 0x80FF, 0xFFFF])
def test_group_address_rejects_broadcast_or_invalid_ids(address):
    with pytest.raises(ValueError):
        validate_group_address(address)


def test_membership_and_query_frames_keep_existing_crypto(monkeypatch):
    protocol = TelinkProtocol(bytes(6), 'Fulife', '2846')
    monkeypatch.setattr(protocol, '_encrypt_command', lambda data: data)
    protocol.set_target_address(3)
    add = protocol.generate_group_membership(0x8011, True)
    assert add[5:10] == bytes.fromhex('0300d71102')
    assert add[10:13] == bytes.fromhex('011180')
    assert protocol.generate_group_membership(0x8011, False)[10:13] == bytes.fromhex('001180')
    query = protocol.generate_group_query()
    assert query[7] == 0xDD and query[10:12] == b'\x08\x01'
    protocol.set_target_address(0x8011)
    with pytest.raises(ValueError):
        protocol.generate_group_membership(0x8011, True)


@pytest.mark.parametrize('payload,expected,complete', [
    ('1112ff00000000000000', [0x8011, 0x8012], True),
    ('ff000000000000000000', [], True),
    ('1112131415161718191a', list(range(0x8011, 0x801B)), False),
])
def test_group_reply_parser(monkeypatch, payload, expected, complete):
    protocol = TelinkProtocol(bytes(6), 'Fulife', '2846')
    protocol.session_key = bytes(16)
    data = bytearray(20)
    data[3:5] = b'\x03\x00'
    data[7:10] = bytes.fromhex('d41102')
    data[10:20] = bytes.fromhex(payload)
    monkeypatch.setattr(TelinkCrypto, 'decrypt_response', lambda *_: data)
    reply = protocol.parse_notification(bytearray(20))
    assert reply['group_addresses'] == expected
    assert reply['groups_complete'] is complete


@pytest.mark.asyncio
async def test_unsolicited_and_foreign_membership_cannot_create_groups(hub):
    hub.protocol.parse_notification = lambda _: {'opcode': 0xD4, 'source': 92,
                                                'group_addresses': [0x8011], 'groups_complete': True}
    hub._process_notification(bytearray(20))
    assert not hub.mesh_groups and not hub.light_groups


@pytest.mark.asyncio
async def test_correlated_reply_discovers_group_and_replaces_membership(hub):
    hub.light_groups[3] = {0x8012}
    future = asyncio.get_running_loop().create_future()
    hub._group_reply_pending[3] = future
    hub.protocol.parse_notification = lambda _: {'opcode': 0xD4, 'source': 3,
                                                'group_addresses': [0x8011], 'groups_complete': True}
    hub._process_notification(bytearray(20))
    assert await future == {0x8011}
    assert hub.light_groups[3] == {0x8011}
    assert hub.mesh_groups[0x8011]['name'] == 'Group 17'
    hub._new_group_callback.assert_called_once_with(0x8011)
    assert hub.get_group_members(0x8011) == {3}


@pytest.mark.asyncio
async def test_partial_reply_retains_membership_without_claiming_confirmation(hub):
    hub.light_groups[3] = {0x8012}
    future = asyncio.get_running_loop().create_future()
    hub._group_reply_pending[3] = future
    hub.protocol.parse_notification = lambda _: {'opcode': 0xD4, 'source': 3,
                                                'group_addresses': [0x8011], 'groups_complete': False}
    hub._process_notification(bytearray(20))
    assert await future is None
    assert hub.light_groups[3] == {0x8011, 0x8012}
    assert 3 not in hub.group_membership_verified


@pytest.mark.asyncio
async def test_membership_write_requires_confirmation_readback(hub, monkeypatch):
    monkeypatch.setattr('custom_components.btalliance.coordinator.asyncio.sleep', AsyncMock())
    hub.async_query_groups = AsyncMock(return_value={0x8011})
    await hub.async_set_group_membership(3, 0x8011, True)
    assert hub.last_group_operation['status'] == 'verified'
    assert hub._async_send_command.call_args.args[0] == 3
    assert hub._async_send_command.call_args.kwargs == {'burst_count': 1}
    hub.async_query_groups.return_value = None
    with pytest.raises(HomeAssistantError, match='unconfirmed'):
        await hub.async_set_group_membership(3, 0x8011, False)
    assert hub.last_group_operation['status'] == 'unconfirmed'


@pytest.mark.asyncio
async def test_membership_rejects_unenrolled_light_without_writing(hub):
    with pytest.raises(HomeAssistantError):
        await hub.async_set_group_membership(92, 0x8011, True)
    hub._async_send_command.assert_not_called()


def test_aggregate_state_does_not_invent_uniform_color_or_output():
    now = time.time()
    states = {3: {'is_on': True, 'luminance': 50, 'red': 255, 'color_mode': 'rgb', 'last_seen': now},
              16: {'is_on': False, 'luminance': 0, 'red': 0, 'color_mode': 'rgb', 'last_seen': now}}
    state = aggregate_group_state({3, 16}, states)
    assert state['is_on'] is True and state['mixed_state'] is True
    assert 'luminance' not in state and 'red' not in state
    states[3]['online'] = False
    assert aggregate_group_state({3, 16}, states)['is_on'] is None
    states[16]['last_seen'] = now - 500
    assert aggregate_group_state({3, 16}, states)['known_member_count'] == 0


@pytest.mark.asyncio
async def test_group_color_targets_group_not_broadcast_or_each_light():
    entity = object.__new__(BTAllianceGroupLight)
    entity._mesh_addr = 0x8011
    entity.coordinator = SimpleNamespace(async_set_rgb=AsyncMock(return_value=True), get_group_members=lambda _: {3})
    entity.async_write_ha_state = Mock()
    await entity.async_turn_on(rgb_color=(128, 0, 255))
    entity.coordinator.async_set_rgb.assert_awaited_once_with(0x8011, 128, 0, 255)


@pytest.mark.asyncio
async def test_empty_group_cannot_send_controls():
    entity = object.__new__(BTAllianceGroupLight)
    entity._mesh_addr = 0x8011
    entity.coordinator = SimpleNamespace(async_turn_off=AsyncMock(), get_group_members=lambda _: set(), login_valid=True)
    assert entity.available is False
    with pytest.raises(HomeAssistantError, match='confirmed enrolled member'):
        await entity.async_turn_off()
    entity.coordinator.async_turn_off.assert_not_called()
