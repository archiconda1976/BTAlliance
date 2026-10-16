"""Network isolation, provisioning failure, exclusions, and setup regressions."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.btalliance import config_flow, discovery, network_management
from custom_components.btalliance import _get_coordinator, BROADCAST_BRIGHTNESS_SCHEMA
from custom_components.btalliance.const import COMMAND_UUID, START_SESSION_UUID
from custom_components.btalliance.coordinator import BTAllianceMeshCoordinator
from custom_components.btalliance.protocol import TelinkProtocol

MAC = "FF:05:01:0B:A4:37"
OTHER = "FF:05:01:0B:A4:38"
CONFIRMATION = bytes.fromhex("07d41f1bcc9c8b95a1842af6db5a4d10ec")


def info(mac=MAC, network="Fulife", address=3):
    data = bytes.fromhex("a0ff00000000040000") + address.to_bytes(2, "little")
    return SimpleNamespace(address=mac, name=network, rssi=-45, manufacturer_data={0x0211: data}, service_uuids=[])


def protocol():
    instance = TelinkProtocol(bytes(6), "Fulife", "2846")
    instance.session_key = bytes(range(16))
    return instance


def test_pairing_packets_match_independent_aes_vectors():
    packets = protocol().generate_network_packets("Archibald", "2846")
    assert [packet.hex() for packet in packets] == [
        "040c0b2f67fb60f8cb303d19c88c9b61de",
        "05047f022aff5f68442479f376e0192381",
        "0651584977a80a9697e340212218be9e11",
    ]
    assert protocol().validate_network_confirmation(CONFIRMATION, "Archibald", "2846")
    assert not protocol().validate_network_confirmation(CONFIRMATION, "Other", "2846")
    assert not protocol().validate_network_confirmation(CONFIRMATION[:-1], "Archibald", "2846")


@pytest.mark.parametrize("name", ["", "abcdefghijk", "\x00", "\u00e9" * 6])
def test_network_name_validation_uses_bytes(name):
    with pytest.raises(ValueError):
        protocol().generate_network_packets(name, "2846")


def test_discovery_reads_secured_network_and_physical_address():
    result = discovery.advertisement(info(network="Archibald", address=128))
    assert result["mesh_name"] == "Archibald"
    assert result["mesh_address"] == 128
    assert result["device_type"] == 0xA0FF
    assert result["mac_address"] == MAC


def test_unrelated_bluetooth_and_short_advertisements():
    unrelated = SimpleNamespace(address="AA:BB:CC:DD:EE:FF", name="Speaker", rssi=-30,
                                manufacturer_data={1: b"123"}, service_uuids=[])
    assert discovery.advertisement(unrelated) is None
    short = info()
    short.manufacturer_data = {0x0211: b"123"}
    assert "mesh_address" not in discovery.advertisement(short)


@pytest.mark.parametrize("value", ["0", "255", "16,bad", "65535"])
def test_invalid_exclusions_are_not_silently_ignored(value):
    with pytest.raises(ValueError):
        discovery.parse_addresses(value)


@pytest.mark.asyncio
async def test_config_flow_preserves_network_credentials_and_filters_gateways(monkeypatch, tmp_path):
    hass = HomeAssistant(str(tmp_path))
    monkeypatch.setattr(discovery.bluetooth, "async_discovered_service_info", lambda _: [info(), info(OTHER, "Archibald")])
    flow = config_flow.BTAllianceConfigFlow()
    flow.hass = hass
    flow.context = {}
    monkeypatch.setattr(flow, "_async_current_entries", lambda: [])
    monkeypatch.setattr(flow, "async_set_unique_id", AsyncMock())
    monkeypatch.setattr(flow, "_abort_if_unique_id_configured", Mock())
    form = await flow.async_step_user()
    assert form["data_schema"]({"mesh_name": "Archibald", "password": "nondefault"})["mesh_name"] == "Archibald"
    picked = await flow.async_step_user({"mesh_name": "Archibald", "password": "nondefault"})
    assert picked["step_id"] == "pick_device"
    with pytest.raises(Exception):
        picked["data_schema"]({"address": MAC})
    result = await flow.async_step_pick_device({"address": OTHER})
    assert result["data"]["password"] == "nondefault"
    assert result["data"]["mesh_name"] == "Archibald"


@pytest.mark.asyncio
async def test_second_network_allowed_but_same_network_rejected(monkeypatch, tmp_path):
    flow = config_flow.BTAllianceConfigFlow()
    flow.hass = HomeAssistant(str(tmp_path))
    flow.context = {}
    existing = SimpleNamespace(data={"mesh_name": "Fulife"}, options={})
    monkeypatch.setattr(flow, "_async_current_entries", lambda: [existing])
    monkeypatch.setattr(discovery.bluetooth, "async_discovered_service_info", lambda _: [info(OTHER, "Archibald")])
    same = await flow.async_step_user({"mesh_name": "Fulife", "password": "2846"})
    assert same["reason"] == "already_configured"
    other = await flow.async_step_user({"mesh_name": "Archibald", "password": "2846"})
    assert other["step_id"] == "pick_device"


@pytest.fixture
def transport(monkeypatch):
    client = SimpleNamespace(write_gatt_char=AsyncMock(), read_gatt_char=AsyncMock(), disconnect=AsyncMock())
    connection = AsyncMock(return_value=client)
    lookup = Mock(return_value=object())
    monkeypatch.setattr(network_management, "establish_connection", connection)
    monkeypatch.setattr(network_management, "async_ble_device_from_address", lookup)
    def login(instance, *_):
        instance.session_key = bytes(range(16))
        return True
    monkeypatch.setattr(TelinkProtocol, "process_login_response", login)
    return client, connection, lookup


@pytest.mark.asyncio
async def test_move_uses_only_selected_mac_and_pairing_characteristic(transport):
    client, connection, lookup = transport
    client.read_gatt_char.side_effect = [bytes(17), CONFIRMATION]
    result = await network_management.manage_device(object(), MAC, "Fulife", "2846", "move", "Archibald")
    assert lookup.call_args.args[1] == MAC
    assert connection.call_args.args[2] == MAC
    assert connection.await_count == 1
    assert [call.args[1][0] for call in client.write_gatt_char.call_args_list] == [0x0C, 0x04, 0x05, 0x06]
    assert all(call.args[0] == str(START_SESSION_UUID) for call in client.write_gatt_char.call_args_list)
    assert result["status"] == "pairing_confirmed"
    assert result["reconnect_verified"] is False
    client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_bad_confirmation_does_not_retry_or_claim_success(transport):
    client, connection, _ = transport
    client.read_gatt_char.side_effect = [bytes(17), b"\x07" + bytes(16)]
    with pytest.raises(HomeAssistantError, match="not confirmed"):
        await network_management.manage_device(object(), MAC, "Fulife", "2846", "move", "Archibald")
    assert client.write_gatt_char.await_count == 4
    connection.assert_awaited_once()
    client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_authentication_failure_prevents_provisioning(transport, monkeypatch):
    client, _, _ = transport
    client.read_gatt_char.return_value = bytes(17)
    monkeypatch.setattr(TelinkProtocol, "process_login_response", lambda *_: False)
    with pytest.raises(HomeAssistantError, match="Authentication"):
        await network_management.manage_device(object(), MAC, "Fulife", "wrong", "move", "Archibald")
    assert client.write_gatt_char.await_count == 1
    client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_reset_is_direct_pairing_delete_never_mesh_e3(transport):
    client, _, _ = transport
    client.read_gatt_char.side_effect = [bytes(17), b"\x0b"]
    result = await network_management.manage_device(object(), MAC, "Archibald", "2846", "reset")
    assert [call.args[1][0] for call in client.write_gatt_char.call_args_list] == [0x0C, 0x0A]
    assert all(call.args[0] != str(COMMAND_UUID) for call in client.write_gatt_char.call_args_list)
    assert result["mesh_name"] == "Fulife"


@pytest.mark.asyncio
async def test_reset_disconnect_during_read_is_reported_uncertain(transport):
    client, _, _ = transport
    client.read_gatt_char.side_effect = [bytes(17), TimeoutError()]
    with pytest.raises(HomeAssistantError, match="uncertain"):
        await network_management.manage_device(object(), MAC, "Archibald", "2846", "reset")
    assert client.write_gatt_char.await_count == 2
    client.disconnect.assert_awaited_once()


@pytest.fixture
def coordinator():
    hub = object.__new__(BTAllianceMeshCoordinator)
    hub.entry = SimpleNamespace(entry_id="test", options={})
    hub.mesh_name = "Fulife"
    hub.device_metadata = {MAC: {"mac_address": MAC, "mesh_name": "Fulife", "mesh_address": 3}}
    hub.firmware_names = {}
    hub.excluded_mesh_addresses = set()
    hub.excluded_mac_addresses = set()
    hub.infrastructure_mesh_addresses = {55}
    hub.cached_light_mesh_addresses = {3, 16}
    hub.enrolled_mesh_addresses = {3, 16}
    hub.discovery_mode = 'approval'
    hub._schedule_pending_light_validation = Mock()
    hub._new_device_callback = Mock()
    hub.observed_mesh_addresses = {}
    hub.mesh_groups = {}
    hub.light_groups = {}
    hub.group_membership_verified = {}
    hub._group_reply_pending = {}
    hub._new_group_callback = None
    hub._persist_groups = Mock()
    hub.discovered_devices = {}
    hub.validated_light_mesh_addresses = set()
    hub.light_states = {}
    hub.pending_discovered_devices = {}
    hub._persist_cached_light_addresses = Mock()
    hub._persist_device_metadata = Mock()
    hub._remove_light_entity_and_device = Mock()
    hub._notify_state_change = Mock()
    hub._remove_infrastructure_light_entity = Mock()
    hub._persist_infrastructure_addresses = Mock()
    hub.infrastructure_states = {}
    hub._new_infrastructure_callback = Mock()
    return hub


def test_exclusion_removes_cache_registry_and_blocks_rediscovery(coordinator):
    coordinator.apply_exclusions({16}, {MAC})
    assert coordinator.cached_light_mesh_addresses == set()
    assert coordinator._remove_light_entity_and_device.call_count == 2
    assert not coordinator._is_controllable_light_address(3)
    coordinator.add_cached_light_address(3)
    coordinator.add_cached_light_address(16)
    assert coordinator.cached_light_mesh_addresses == set()


def test_unenrolled_status_does_not_create_or_query_light(coordinator):
    coordinator.protocol = SimpleNamespace(
        parse_notification=lambda _: {"opcode": 0xDB, "source": 92},
        get_target_address=lambda: 0xFFFF,
    )
    coordinator._process_notification(bytearray(20))
    assert 92 in coordinator.observed_mesh_addresses
    assert 92 not in coordinator.light_states
    coordinator.add_cached_light_address(92)
    assert 92 not in coordinator.cached_light_mesh_addresses


def test_enrollment_removes_old_entities_and_caches_owned_nodes(coordinator):
    coordinator.apply_enrollment({3, 237})
    assert coordinator.cached_light_mesh_addresses == {3, 237}
    coordinator._remove_light_entity_and_device.assert_called_once_with(16)


def test_offline_report_preserves_last_output_and_never_discovers(coordinator):
    coordinator.light_states[3] = {"is_on": True, "luminance": 50}
    coordinator._process_mesh_status({"light_addr": 3, "online": False,
                                    "is_on": False, "luminance": 100, "status_counter": 0})
    assert coordinator.light_states[3] == {"is_on": True, "luminance": 50, "online": False}
    assert not coordinator.pending_discovered_devices


def test_status_parser_reads_two_records_and_offline_counter(monkeypatch):
    from custom_components.btalliance.protocol import TelinkCrypto
    packet = bytearray(20)
    packet[7:10] = bytes.fromhex("dc1102")
    packet[10:18] = bytes([3, 0, 100, 0, 16, 2, 50, 0])
    monkeypatch.setattr(TelinkCrypto, "decrypt_response", lambda *_: packet)
    parsed = protocol().parse_notification(bytearray(20))
    assert len(parsed['records']) == 2
    assert parsed['records'][0]['online'] is False
    assert parsed['records'][1]['light_addr'] == 16
    assert parsed['records'][1]['is_on'] is True
    assert 'destination' not in parsed
    packet[8:10] = b'\x00\x00'
    assert protocol().parse_notification(bytearray(20)) is None


def test_status_parser_accepts_captured_vendor_and_color(monkeypatch):
    from custom_components.btalliance.protocol import TelinkCrypto
    captured = bytearray.fromhex('00515b03006bb2db11027facff00000064000500')
    monkeypatch.setattr(TelinkCrypto, 'decrypt_response', lambda *_: captured)
    parsed = protocol().parse_notification(bytearray(20))
    assert parsed['source'] == 3
    assert (parsed['red'], parsed['green'], parsed['blue']) == (127, 172, 255)
    assert parsed['luminance'] == 100


def test_both_mesh_records_update_enrolled_lights(coordinator):
    coordinator.protocol = SimpleNamespace(parse_notification=lambda _: {
        'opcode': 0xDC, 'source': 0, 'mic': '0000', 'raw': bytearray(20),
        'records': [
            {'light_addr': 3, 'online': True, 'status_counter': 1, 'is_on': True, 'luminance': 50},
            {'light_addr': 16, 'online': True, 'status_counter': 2, 'is_on': False, 'luminance': 0},
        ],
    })
    coordinator._process_notification(bytearray(20))
    assert coordinator.light_states[3]['luminance'] == 50
    assert coordinator.light_states[16]['is_on'] is False
    assert set(coordinator.observed_mesh_addresses) == {3, 16}


@pytest.mark.asyncio
async def test_queries_use_single_write_while_control_bursts_remain(coordinator):
    coordinator._async_send_command = AsyncMock(return_value=True)
    coordinator.protocol = SimpleNamespace(generate_query_status_command=Mock(),
                                           generate_firmware_query=Mock(), generate_on_off_command=Mock())
    coordinator._firmware_requested = set()
    await coordinator.async_query_status(3)
    assert coordinator._async_send_command.call_args.kwargs == {'burst_count': 1}
    await coordinator.async_query_firmware(3)
    assert coordinator._async_send_command.call_args.kwargs == {'burst_count': 1}
    await coordinator.async_turn_off(3)
    assert coordinator._async_send_command.call_args.kwargs == {}


def test_colliding_addresses_do_not_claim_unique_mac_or_firmware(coordinator):
    coordinator.device_metadata[OTHER] = {"mac_address": OTHER, "mesh_name": "Fulife", "mesh_address": 3}
    coordinator.firmware_names[3] = "E04010"
    metadata = coordinator.get_device_metadata(3)
    assert metadata["address_collision"] is True
    assert metadata["mac_addresses"] == [MAC, OTHER]
    assert metadata["firmware"] is None
    assert metadata["mac_address"] is None
    assert metadata["mac_address_status"] == "ambiguous"


def test_mac_attribute_distinguishes_unknown_and_matched(coordinator):
    metadata = coordinator.get_device_metadata(3)
    assert metadata['mac_address'] == MAC
    assert metadata['mac_address_source'] == 'advertisement'
    assert metadata['mac_address_status'] == 'matched'
    metadata = coordinator.get_device_metadata(16)
    assert metadata['mac_address'] is None
    assert metadata['mac_addresses'] == []
    assert metadata['mac_address_status'] == 'not_found'


def test_advertisement_identity_change_notifies_entities(coordinator, monkeypatch):
    coordinator.hass = SimpleNamespace()
    monkeypatch.setattr('custom_components.btalliance.coordinator.discovered_devices',
                        lambda _: {MAC: {'mac_address': MAC, 'mesh_name': 'Fulife', 'mesh_address': 16, 'rssi': -50}})
    coordinator.refresh_advertisement_metadata()
    assert coordinator._notify_state_change.call_args_list == [((3,),), ((16,),)]
    assert coordinator.get_device_metadata(16)['mac_address'] == MAC
    coordinator._notify_state_change.reset_mock()
    coordinator.refresh_advertisement_metadata()
    coordinator._notify_state_change.assert_not_called()


def test_firmware_notification_updates_attributes_without_creating_light(coordinator):
    coordinator.protocol = SimpleNamespace(parse_notification=lambda _: {"opcode": 0xC8, "source": 3, "firmware_name": "E04010"})
    coordinator._process_notification(bytearray(20))
    assert coordinator.get_device_metadata(3)["firmware_name"] == "E04010"
    assert coordinator.discovered_devices == {}
    coordinator.apply_exclusions({3}, set())
    coordinator.protocol.parse_notification = lambda _: {"opcode": 0xC8, "source": 3, "firmware_name": "J0101H"}
    coordinator._process_notification(bytearray(20))
    assert coordinator.firmware_names[3] == "E04010"


@pytest.mark.asyncio
async def test_services_respect_explicit_hub_and_reject_unknown_hub():
    hub = object()
    hass = SimpleNamespace(data={"btalliance": {"test": hub}})
    call = SimpleNamespace(data={"entry_id": "missing"})
    with pytest.raises(HomeAssistantError, match="Unknown"):
        await _get_coordinator(hass, call)
    call.data = {"entry_id": "test"}
    assert await _get_coordinator(hass, call) is hub
    assert BROADCAST_BRIGHTNESS_SCHEMA({"entry_id": "test", "brightness": 128})["entry_id"] == "test"


def test_gateway_fallback_stays_on_network_and_respects_exclusions(coordinator, monkeypatch):
    coordinator.gateway_address = int(MAC.replace(":", ""), 16)
    coordinator.gateway_candidate_failures = {}
    coordinator.gateway_candidates = {}
    coordinator.hass = object()
    coordinator.excluded_mac_addresses = {MAC}
    neighbor = "FF:05:01:0B:A4:39"
    monkeypatch.setattr(discovery.bluetooth, "async_discovered_service_info", lambda _: [info(), info(OTHER, address=16), info(neighbor, "Archibald")])
    assert coordinator._candidate_gateway_addresses() == [OTHER]


def test_delayed_advertisement_cannot_undo_confirmed_network_change(coordinator, monkeypatch):
    coordinator.hass = object()
    coordinator.device_metadata[MAC]["mesh_name"] = "Archibald"
    coordinator.device_metadata[MAC]["pending_network_change"] = True
    coordinator.device_metadata[MAC]["pending_network_change_until"] = 200
    monkeypatch.setattr('custom_components.btalliance.coordinator.time.time', lambda: 100)
    monkeypatch.setattr(discovery.bluetooth, "async_discovered_service_info", lambda _: [info()])
    coordinator.refresh_advertisement_metadata()
    assert coordinator.device_metadata[MAC]["mesh_name"] == "Archibald"
    coordinator.mesh_name = "Archibald"
    monkeypatch.setattr(discovery.bluetooth, "async_discovered_service_info", lambda _: [info(network="Archibald")])
    coordinator.refresh_advertisement_metadata()
    assert "pending_network_change" not in coordinator.device_metadata[MAC]
    assert "pending_network_change_until" not in coordinator.device_metadata[MAC]


def test_expired_migration_marker_accepts_external_return(coordinator, monkeypatch):
    coordinator.hass = object()
    coordinator.device_metadata[MAC].update(mesh_name='Archibald', pending_network_change=True,
                                             pending_network_change_until=90)
    monkeypatch.setattr('custom_components.btalliance.coordinator.time.time', lambda: 100)
    monkeypatch.setattr(discovery.bluetooth, 'async_discovered_service_info', lambda _: [info()])
    coordinator.refresh_advertisement_metadata()
    assert coordinator.device_metadata[MAC]['mesh_name'] == 'Fulife'
    assert 'pending_network_change' not in coordinator.device_metadata[MAC]


def test_legacy_migration_marker_does_not_block_external_return(coordinator, monkeypatch):
    coordinator.hass = object()
    coordinator.device_metadata[MAC].update(mesh_name='Archibald', pending_network_change=True)
    monkeypatch.setattr(discovery.bluetooth, 'async_discovered_service_info', lambda _: [info()])
    coordinator.refresh_advertisement_metadata()
    assert coordinator.device_metadata[MAC]['mesh_name'] == 'Fulife'


def online_record(address=92, counter=1):
    return {'light_addr': address, 'online': True, 'status_counter': counter,
            'is_on': True, 'luminance': 50, 'raw': bytearray(20), 'source': 0, 'mic': '0000'}


def test_automatic_discovery_requires_distinct_online_reports(coordinator):
    coordinator.discovery_mode = 'automatic'
    coordinator._process_mesh_status(online_record())
    coordinator._process_mesh_status(online_record())
    assert 92 not in coordinator.cached_light_mesh_addresses
    assert coordinator.pending_discovered_devices[92]['reports'] == 1
    coordinator._process_mesh_status(online_record(counter=2))
    assert 92 in coordinator.cached_light_mesh_addresses
    assert 92 in coordinator.enrolled_mesh_addresses
    assert 92 not in coordinator.pending_discovered_devices
    coordinator._new_device_callback.assert_called_once_with(92)


def test_automatic_discovery_accepts_full_status_without_preapproval(coordinator):
    coordinator.discovery_mode = 'automatic'
    coordinator.protocol = SimpleNamespace(parse_notification=lambda _: {
        'opcode': 0xDB, 'source': 92, 'is_on': True, 'luminance': 50,
        'red': 100, 'green': 0, 'blue': 0, 'color_temp': 0, 'warm': 0, 'cool': 0,
        'raw': bytearray(20)}, get_target_address=lambda: 0xFFFF)
    coordinator._process_notification(bytearray(20))
    assert 92 in coordinator.cached_light_mesh_addresses
    coordinator._new_device_callback.assert_called_once_with(92)


@pytest.mark.parametrize('address', [0, 255, 65535])
def test_automatic_discovery_rejects_invalid_full_status_source(coordinator, address):
    coordinator.discovery_mode = 'automatic'
    coordinator.protocol = SimpleNamespace(parse_notification=lambda _: {
        'opcode': 0xDB, 'source': address}, get_target_address=lambda: 0xFFFF)
    coordinator._process_notification(bytearray(20))
    assert address not in coordinator.cached_light_mesh_addresses


def test_automatic_discovery_respects_exclusion_and_offline_reports(coordinator):
    coordinator.discovery_mode = 'automatic'
    coordinator.excluded_mesh_addresses = {92}
    for counter in (1, 2):
        coordinator._process_mesh_status(online_record(counter=counter))
    coordinator._process_mesh_status({**online_record(93), 'online': False, 'status_counter': 0})
    assert 92 not in coordinator.cached_light_mesh_addresses
    assert 93 not in coordinator.cached_light_mesh_addresses
    assert not coordinator.pending_discovered_devices


def test_automatic_discovery_does_not_assume_address_55_is_relay(coordinator):
    coordinator.discovery_mode = 'automatic'
    coordinator.infrastructure_mesh_addresses = set()
    for counter in (1, 2):
        coordinator._process_mesh_status(online_record(55, counter))
    assert 55 in coordinator.cached_light_mesh_addresses
    assert not coordinator.is_infrastructure_address(55)


def test_advertised_relay_is_infrastructure_at_any_address(coordinator, monkeypatch):
    coordinator.discovery_mode = 'automatic'
    coordinator.hass = object()
    node = info(MAC, address=4)
    node.manufacturer_data[0x0211] = b'\xa0\x13' + node.manufacturer_data[0x0211][2:]
    monkeypatch.setattr(discovery.bluetooth, 'async_discovered_service_info', lambda _: [node])
    coordinator.refresh_advertisement_metadata()
    assert 4 in coordinator.infrastructure_mesh_addresses
    coordinator._new_infrastructure_callback.assert_called_once_with(4)
    for counter in (1, 2):
        coordinator._process_mesh_status(online_record(4, counter))
    assert 4 not in coordinator.cached_light_mesh_addresses
    coordinator._new_device_callback.assert_not_called()


def test_gateway_fallback_prefers_relay_over_stronger_light(coordinator, monkeypatch):
    coordinator.gateway_address = int(MAC.replace(':', ''), 16)
    coordinator.gateway_candidate_failures = {}
    coordinator.gateway_candidates = {}
    coordinator.hass = object()
    coordinator.excluded_mac_addresses = {MAC}
    relay_mac = 'FF:05:01:0B:A4:39'
    relay = info(relay_mac, address=4)
    relay.rssi = -80
    relay.manufacturer_data[0x0211] = b'\xa0\x13' + relay.manufacturer_data[0x0211][2:]
    light = info(OTHER, address=16)
    light.rssi = -30
    monkeypatch.setattr(discovery.bluetooth, 'async_discovered_service_info', lambda _: [light, relay])
    assert coordinator._candidate_gateway_addresses() == [relay_mac, OTHER]
    assert coordinator.gateway_candidates[relay_mac]['preferred_relay'] is True
    coordinator.excluded_mac_addresses = set()
    assert coordinator._candidate_gateway_addresses()[0] == MAC


@pytest.mark.asyncio
async def test_validation_queue_is_bounded_and_entry_owned(coordinator):
    coordinator.discovery_mode = 'automatic'
    coordinator.hass = object()
    coordinator.pending_discovered_devices = {92: {'reports': 1}}
    coordinator._discovery_validation_tasks = {Mock(), Mock(), Mock()}
    def create_task(hass, coroutine, name):
        coroutine.close()
        return Mock()
    coordinator.entry.async_create_background_task = Mock(side_effect=create_task)
    schedule = BTAllianceMeshCoordinator._schedule_pending_light_validation
    schedule(coordinator, 92)
    coordinator.entry.async_create_background_task.assert_not_called()
    coordinator._discovery_validation_tasks.clear()
    schedule(coordinator, 92)
    assert len(coordinator._discovery_validation_tasks) == 1
    assert coordinator.pending_discovered_devices[92]['validating'] is True
    assert coordinator.entry.async_create_background_task.call_args.args[2] == 'btalliance_validate_92'
    schedule(coordinator, 92)
    coordinator.entry.async_create_background_task.assert_called_once()


@pytest.mark.asyncio
async def test_setup_defaults_to_automatic_and_marks_preferred_relays(monkeypatch, tmp_path):
    flow = config_flow.BTAllianceConfigFlow()
    flow.hass = HomeAssistant(str(tmp_path))
    flow.context = {}
    monkeypatch.setattr(flow, '_async_current_entries', lambda: [])
    relay = info(MAC, 'Archibald', address=5)
    relay.rssi = -80
    relay.manufacturer_data[0x0211] = b'\xa0\x13' + relay.manufacturer_data[0x0211][2:]
    light = info(OTHER, 'Archibald', address=2)
    light.rssi = -30
    monkeypatch.setattr(discovery.bluetooth, 'async_discovered_service_info', lambda _: [light, relay])
    form = await flow.async_step_user()
    settings = form['data_schema']({'mesh_name': 'Archibald', 'password': '2846'})
    assert settings['discovery_mode'] == 'automatic'
    picked = await flow.async_step_user(settings)
    field = next(iter(picked['data_schema'].schema.values()))
    options = field.config['options']
    assert options[0]['value'] == MAC
    assert 'preferred gateway' in options[0]['label']
    assert 'mesh 5' in options[0]['label']
    assert picked['data_schema']({})['address'] == MAC


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,expected', [('automatic', {3, 16}), ('approval', set())])
async def test_cached_lights_survive_empty_approval_list_only_in_automatic_mode(tmp_path, mode, expected):
    entry = SimpleNamespace(entry_id='test', options={'discovery_mode': mode,
        'enrolled_mesh_addresses': ''}, data={}, async_on_unload=Mock())
    instance = BTAllianceMeshCoordinator(HomeAssistant(str(tmp_path)), entry, 1, 'Archibald',
                                       '2846', cached_light_mesh_addresses={3, 16})
    assert instance.cached_light_mesh_addresses == expected
    assert instance.enrolled_mesh_addresses == expected


@pytest.mark.asyncio
async def test_move_rejects_visible_destination_address_collision(coordinator, monkeypatch):
    coordinator.hass = object()
    monkeypatch.setattr(discovery.bluetooth, "async_discovered_service_info", lambda _: [info(), info(OTHER, "Archibald", 3)])
    with pytest.raises(HomeAssistantError, match="collision"):
        await coordinator.async_manage_device(MAC, "move", "Archibald")


def options_flow(hass, entry, hub):
    hass.config_entries = SimpleNamespace(async_get_known_entry=lambda _: entry, async_entries=lambda _: [entry])
    hass.data["btalliance"] = {"test": hub}
    flow = config_flow.BTAllianceOptionsFlow()
    flow.hass = hass
    flow.handler = "test"
    flow.context = {}
    return flow


@pytest.mark.asyncio
async def test_options_require_confirmation_and_preserve_cached_options(monkeypatch, tmp_path):
    hass = HomeAssistant(str(tmp_path))
    entry = SimpleNamespace(entry_id="test", data={"mesh_name": "Fulife", "password": "2846"},
                            options={"discovered_light_mesh_addresses": "3,16", "device_metadata": {MAC: {"mesh_address": 3}}})
    hub = SimpleNamespace(mesh_name="Fulife", async_manage_device=AsyncMock())
    flow = options_flow(hass, entry, hub)
    monkeypatch.setattr(discovery.bluetooth, "async_discovered_service_info", lambda _: [info()])
    form = await flow.async_step_manage_device()
    validated = form["data_schema"]({"mac_address": MAC, "operation": "move", "new_mesh_name": "Archibald"})
    confirm = await flow.async_step_manage_device(validated)
    assert confirm["step_id"] == "confirm_device"
    await flow.async_step_confirm_device({"confirm": False})
    hub.async_manage_device.assert_not_awaited()
    result = await flow.async_step_confirm_device({"confirm": True})
    hub.async_manage_device.assert_awaited_once_with(MAC, "move", "Archibald", "2846")
    assert result["data"] == entry.options


@pytest.mark.asyncio
async def test_option_settings_preserve_identity_and_cache(monkeypatch, tmp_path):
    hass = HomeAssistant(str(tmp_path))
    entry = SimpleNamespace(entry_id="test", data={"mesh_name": "Fulife", "password": "2846"},
                            options={"device_metadata": {MAC: {"mesh_address": 3}}, "firmware_cache": {"names": {"3": "E04010"}}})
    hub = SimpleNamespace(mesh_name="Fulife", apply_exclusions=Mock(), apply_enrollment=Mock(), cached_light_mesh_addresses={3})
    flow = options_flow(hass, entry, hub)
    monkeypatch.setattr(discovery.bluetooth, "async_discovered_service_info", lambda _: [info()])
    form = await flow.async_step_settings()
    settings = form["data_schema"]({"mesh_name": "Fulife", "password": "2846", "excluded_mesh_addresses": "16"})
    result = await flow.async_step_settings(settings)
    assert result["data"]["device_metadata"] == entry.options["device_metadata"]
    assert result["data"]["firmware_cache"] == entry.options["firmware_cache"]
    assert result["data"]["discovered_light_mesh_addresses"] == "3"
    hub.apply_exclusions.assert_called_once_with({16}, set())
