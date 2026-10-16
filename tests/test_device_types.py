"""App-reference type translation and metadata provenance."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from custom_components.btalliance.coordinator import BTAllianceMeshCoordinator
from custom_components.btalliance.device_types import TYPE_NAMES, device_type_name
from custom_components.btalliance.discovery import advertisement
from custom_components.btalliance import async_setup_entry
from custom_components.btalliance.const import CONF_GATEWAY_ADDRESS, CONF_MESH_NAME, CONF_PASSWORD


@pytest.mark.parametrize('subtype,name', TYPE_NAMES.items())
def test_app_type_table(subtype, name):
    for family in range(0xA0, 0xA7):
        assert device_type_name((family << 8) | subtype) == name


@pytest.mark.parametrize('code,name', [(0xC000, 'HandControl'), (0xC001, 'PanelSwitch'),
    (0xC100, 'Gateway'), (0xC301, 'Switch'), (0xC402, 'Socket')])
def test_special_app_families(code, name):
    assert device_type_name(code) == name


@pytest.mark.parametrize('code', [None, True, -1, 65536, '40968', 0xC002, 0xC101, 0xA0FF, 0xB008])
def test_unknown_type_does_not_invent_model(code):
    assert device_type_name(code) is None


def test_short_advertisement_has_type_but_no_mesh_address():
    result = advertisement(SimpleNamespace(address='FF:00:06:0E:08:1A', name='Fulife', rssi=-80,
        manufacturer_data={0x0211: bytes.fromhex('a0081a080e06')}, service_uuids=[]))
    assert result['device_type'] == 0xA008
    assert 'mesh_address' not in result


def hub():
    instance = object.__new__(BTAllianceMeshCoordinator)
    instance.entry = SimpleNamespace(options={})
    instance.hass = SimpleNamespace(config_entries=SimpleNamespace(async_update_entry=Mock()))
    instance.mesh_name = 'Fulife'
    instance.device_metadata = {}
    instance.enrolled_mesh_addresses = {3}
    instance.excluded_mesh_addresses = set()
    instance.excluded_mac_addresses = set()
    instance.infrastructure_mesh_addresses = {55}
    instance.firmware_names = {}
    instance.light_states = {}
    instance.light_groups = {}
    instance.group_membership_verified = {}
    instance._notify_state_change = Mock()
    return instance


def test_manual_and_app_export_cache_is_not_used():
    instance = hub()
    instance.entry.options['cached_device_types'] = {'mesh_name': 'Fulife',
        'types': {'3': {'device_type': 40968, 'source': 'app_export'}}}
    metadata = instance.get_device_metadata(3)
    assert metadata['device_type_name'] is None
    assert metadata['device_type'] is None
    assert metadata['device_type_source'] is None
    assert metadata['mac_address'] is None


def test_retained_advertisement_type_is_used_without_live_status():
    instance = hub()
    instance.device_metadata['FF:00:06:0E:08:1A'] = {'mac_address': 'FF:00:06:0E:08:1A',
        'mesh_name': 'Fulife', 'mesh_address': 3, 'device_type': 0xA013}
    metadata = instance.get_device_metadata(3)
    assert metadata['device_type_name'] == 'Relay'
    assert metadata['device_type_source'] == 'advertisement'
    assert metadata['mac_address'] == 'FF:00:06:0E:08:1A'
    assert metadata['online'] is None


def test_collision_does_not_select_one_device_model():
    instance = hub()
    for mac, code in [('FF:00:06:0E:08:1A', 0xA008), ('FF:00:06:0E:08:1B', 0xA013)]:
        instance.device_metadata[mac] = {'mac_address': mac, 'mesh_name': 'Fulife', 'mesh_address': 3, 'device_type': code}
    metadata = instance.get_device_metadata(3)
    assert metadata['address_collision'] is True
    assert metadata['device_type_name'] is None


def test_unknown_type_is_not_inferred_from_mesh_address():
    instance = hub()
    for address in (1, 3, 16, 55, 91, 128, 237):
        metadata = instance.get_device_metadata(address)
        assert metadata['device_type'] is None
        assert metadata['device_type_name'] is None


@pytest.mark.asyncio
async def test_setup_discards_only_manual_type_cache():
    retained = {'device_metadata': {'known': {'device_type': 40968}},
                'enrolled_mesh_addresses': '1,3', 'firmware_cache': {'mesh_name': 'Fulife'}}
    entry = SimpleNamespace(entry_id='entry', data={CONF_GATEWAY_ADDRESS: 1,
        CONF_MESH_NAME: 'Fulife', CONF_PASSWORD: '2846'},
        options={**retained, 'cached_device_types': {'types': {'3': {'device_type': 40968}}}},
        async_on_unload=Mock(), add_update_listener=Mock())
    def update_entry(entry, *, options):
        entry.options = options
    hass = SimpleNamespace(data={}, services=Mock(), config_entries=SimpleNamespace(
        async_update_entry=Mock(side_effect=update_entry), async_forward_entry_setups=AsyncMock()))
    with patch('custom_components.btalliance.BTAllianceMeshCoordinator'), \
         patch('custom_components.btalliance.dr.async_get', return_value=Mock()):
        assert await async_setup_entry(hass, entry)
    assert entry.options == retained
    hass.config_entries.async_update_entry.assert_called_once_with(entry, options=retained)
