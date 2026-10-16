"""Physical identity on the Home Assistant device page."""

from types import SimpleNamespace
from unittest.mock import Mock, create_autospec, patch

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo

from custom_components.btalliance.device_info import apply_device_metadata, sync_device_info


def test_known_identity_is_shown_without_changing_identifiers():
    info = DeviceInfo(identifiers={('btalliance', 'entry_1')}, via_device_id='hub')
    apply_device_metadata(info, {'device_type_name': 'BL200-bt',
                                'mac_address': 'FF:00:06:0E:0E:F9'}, 'Mesh Light')
    assert info['model'] == 'BL200-bt'
    assert info['connections'] == {(dr.CONNECTION_BLUETOOTH, 'FF:00:06:0E:0E:F9')}
    assert info['identifiers'] == {('btalliance', 'entry_1')}
    assert info['via_device_id'] == 'hub'


def test_cached_type_without_mac():
    info = DeviceInfo()
    apply_device_metadata(info, {'device_type_name': 'BL200-bt'}, 'Mesh Light')
    assert info['model'] == 'BL200-bt'
    assert info['connections'] == set()


def test_ambiguous_identity_clears_old_mac():
    info = DeviceInfo(connections={(dr.CONNECTION_BLUETOOTH, 'old')}, model='BL200-bt')
    apply_device_metadata(info, {'address_collision': True, 'mac_address': 'old'}, 'Mesh Light')
    assert info['model'] == 'Mesh Light'
    assert info['connections'] == set()


def test_late_metadata_updates_existing_registry_device():
    registry = create_autospec(dr.DeviceRegistry, instance=True)
    registry.async_get_device.return_value = SimpleNamespace(id='device', connections=set(), model='Mesh Light')
    info = DeviceInfo(identifiers={('btalliance', 'entry_1')}, model='BL200-bt',
                      connections={(dr.CONNECTION_BLUETOOTH, 'FF:00:06:0E:0E:F9')})
    with patch('custom_components.btalliance.device_info.dr.async_get', return_value=registry):
        sync_device_info(Mock(), 'entry', info)
    registry.async_update_device.assert_called_once_with('device', new_connections=info['connections'])
    registry.async_get_or_create.assert_called_once_with(config_entry_id='entry', **info)


def test_collision_removes_bluetooth_but_preserves_other_connections():
    registry = create_autospec(dr.DeviceRegistry, instance=True)
    registry.async_get_device.return_value = SimpleNamespace(id='device',
        connections={(dr.CONNECTION_BLUETOOTH, 'old'), ('other', 'preserved')}, model='Mesh Light')
    info = DeviceInfo(identifiers={('btalliance', 'entry_1')}, model='Mesh Light', connections=set())
    with patch('custom_components.btalliance.device_info.dr.async_get', return_value=registry):
        sync_device_info(Mock(), 'entry', info)
    registry.async_update_device.assert_called_once_with('device', new_connections={('other', 'preserved')})
    registry.async_get_or_create.assert_not_called()


def test_unchanged_metadata_does_not_write_registry():
    registry = create_autospec(dr.DeviceRegistry, instance=True)
    registry.async_get_device.return_value = SimpleNamespace(id='device', connections=set(), model='Mesh Light')
    info = DeviceInfo(identifiers={('btalliance', 'entry_1')}, model='Mesh Light', connections=set())
    with patch('custom_components.btalliance.device_info.dr.async_get', return_value=registry):
        sync_device_info(Mock(), 'entry', info)
    registry.async_update_device.assert_not_called()
    registry.async_get_or_create.assert_not_called()
