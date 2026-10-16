"""Imported metadata cannot grant startup enrollment or override output levels."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.btalliance import async_setup_entry
from custom_components.btalliance.coordinator import BTAllianceMeshCoordinator
from custom_components.btalliance.discovery import parse_addresses
from custom_components.btalliance.share_settings import IMPORT_KEY, IMPORT_ENROLLMENT_VERSION, import_enrollment_options
from custom_components.btalliance.light import BTAllianceMeshLight


@pytest.fixture
def imported():
    return {'mesh_name': 'Example', 'devices': {
        '9': {'name': 'Path light', 'device_type': 40968},
        '10': {'name': 'Tree light', 'device_type': 40968},
        '11': {'name': 'Mesh relay', 'device_type': 40979}}, 'groups': {}}


def entry_for(options):
    return SimpleNamespace(entry_id='test', data={'mesh_name': 'Example', 'password': '2846', 'gateway_address': 1},
        options=deepcopy(options), async_on_unload=Mock(), add_update_listener=Mock())


def hub_for(hass, entry):
    return BTAllianceMeshCoordinator(hass, entry, 1, 'Example', '2846',
        cached_light_mesh_addresses=parse_addresses(entry.options.get('discovered_light_mesh_addresses', '')),
        infrastructure_mesh_addresses=parse_addresses(entry.options.get('infrastructure_mesh_addresses', '')),
        excluded_mesh_addresses=parse_addresses(entry.options.get('excluded_mesh_addresses', '')))


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['automatic', 'approval'])
async def test_metadata_only_import_does_not_create_entities(imported, mode, tmp_path):
    hass = HomeAssistant(str(tmp_path))
    entry = entry_for({IMPORT_KEY: imported, 'discovery_mode': mode, 'enrolled_mesh_addresses': ''})
    hub = hub_for(hass, entry)
    assert not hub.cached_light_mesh_addresses and not hub.enrolled_mesh_addresses
    assert not hub.infrastructure_mesh_addresses
    assert hub.get_device_metadata(9)['app_import_name'] == 'Path light'


@pytest.mark.asyncio
async def test_confirmed_import_enrolls_only_new_selection(imported, tmp_path):
    hass = HomeAssistant(str(tmp_path))
    entry = entry_for({'discovery_mode': 'approval', 'enrolled_mesh_addresses': '',
                       'discovered_light_mesh_addresses': '', IMPORT_KEY: imported})
    hub = hub_for(hass, entry)
    # All old records remain metadata, but only selected 9/11 gain enrollment.
    options = {**entry.options, **import_enrollment_options(hub, imported, ['9', '11'])}
    restored = hub_for(hass, entry_for(options))
    assert restored.cached_light_mesh_addresses == {9}
    assert restored.enrolled_mesh_addresses == {9}
    assert restored.infrastructure_mesh_addresses == {11}


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['automatic', 'approval'])
async def test_forget_persists_removal_and_rediscovery_keeps_name(imported, mode, tmp_path):
    hass = HomeAssistant(str(tmp_path))
    hass.config_entries = SimpleNamespace(async_update_entry=lambda entry, **kw: setattr(entry, 'options', kw['options']))
    entry = entry_for({IMPORT_KEY: imported, 'discovery_mode': mode,
        'enrolled_mesh_addresses': '9', 'discovered_light_mesh_addresses': '9'})
    hub = hub_for(hass, entry)
    hub.forget_light_address(9)
    assert entry.options['enrolled_mesh_addresses'] == ''
    assert entry.options['discovered_light_mesh_addresses'] == ''
    restored = hub_for(hass, entry)
    assert not restored.cached_light_mesh_addresses
    assert restored.get_device_metadata(9)['app_import_name'] == 'Path light'
    restored.add_cached_light_address(9)
    assert restored.cached_light_mesh_addresses == ({9} if mode == 'automatic' else set())


@pytest.mark.asyncio
async def test_cleanup_persists_revocation_without_deleting_import_metadata(imported, tmp_path):
    hass = HomeAssistant(str(tmp_path))
    hass.config_entries = SimpleNamespace(async_update_entry=lambda entry, **kw: setattr(entry, 'options', kw['options']))
    entry = entry_for({IMPORT_KEY: imported, 'discovery_mode': 'approval',
        'enrolled_mesh_addresses': '9,10', 'discovered_light_mesh_addresses': '9,10'})
    hub = hub_for(hass, entry)
    hub._remove_light_entity_and_device = Mock()
    assert hub.cleanup_cached_light_addresses(keep_addresses={9}) == [10]
    restored = hub_for(hass, entry)
    assert restored.cached_light_mesh_addresses == {9}
    assert restored.get_device_metadata(10)['app_import_name'] == 'Tree light'


@pytest.mark.asyncio
async def test_revoked_approval_and_exclusions_win_over_import(imported, tmp_path):
    hass = HomeAssistant(str(tmp_path))
    entry = entry_for({IMPORT_KEY: imported, 'discovery_mode': 'approval',
        'enrolled_mesh_addresses': '9', 'discovered_light_mesh_addresses': '9,10',
        'excluded_mesh_addresses': '9'})
    hub = hub_for(hass, entry)
    assert not hub.cached_light_mesh_addresses
    hub.add_cached_light_address(9)
    hub.add_cached_light_address(10)
    assert not hub.cached_light_mesh_addresses


@pytest.mark.asyncio
async def test_legacy_migration_keeps_only_persisted_enrollment(imported):
    entry = entry_for({IMPORT_KEY: imported, 'enrolled_mesh_addresses': '9', 'discovered_light_mesh_addresses': '9'})
    update = Mock(side_effect=lambda entry, **kw: setattr(entry, 'options', kw['options']))
    hass = SimpleNamespace(data={}, services=Mock(), config_entries=SimpleNamespace(
        async_update_entry=update, async_forward_entry_setups=AsyncMock()))
    with patch('custom_components.btalliance.BTAllianceMeshCoordinator'), \
         patch('custom_components.btalliance.dr.async_get', return_value=Mock()), \
         patch('custom_components.btalliance.er.async_get', return_value=Mock()), \
         patch('custom_components.btalliance.er.async_entries_for_config_entry', return_value=[
             SimpleNamespace(domain='binary_sensor', unique_id='test_infrastructure_11')]):
        assert await async_setup_entry(hass, entry)
        assert entry.options[IMPORT_ENROLLMENT_VERSION] == 1
        assert entry.options['enrolled_mesh_addresses'] == '9'
        assert entry.options['discovered_light_mesh_addresses'] == '9'
        assert entry.options['infrastructure_mesh_addresses'] == '11'
        update.reset_mock()
        assert await async_setup_entry(hass, entry)
        update.assert_not_called()


@pytest.mark.asyncio
async def test_registry_removal_releases_discovery_slot():
    light = object.__new__(BTAllianceMeshLight)
    light._mesh_addr = 9
    known = {9}
    light._on_forget = lambda: known.discard(9)
    light.coordinator = SimpleNamespace(forget_light_address=Mock())
    await light.async_removed_from_registry()
    assert not known
    light.coordinator.forget_light_address.assert_called_once_with(9)


@pytest.mark.asyncio
@pytest.mark.parametrize('address', [9, 0x8011, 0xFFFF])
@pytest.mark.parametrize('known_level', [None, 0, 25, 100])
async def test_turn_on_sends_no_implicit_brightness(address, known_level):
    hub = object.__new__(BTAllianceMeshCoordinator)
    hub.light_states = {} if known_level is None else {9: {'luminance': known_level}}
    hub.protocol = Mock()
    hub.protocol.generate_on_off_command.return_value = b'ON'
    sent = []

    async def send(target, factory, description):
        sent.append((target, factory(), description))
        return True

    hub._async_send_command = send
    assert await hub.async_turn_on(address)
    assert sent == [(address, b'ON', 'turn on')]
    hub.protocol.generate_luminance_command.assert_not_called()


@pytest.mark.asyncio
async def test_explicit_brightness_still_uses_requested_level():
    hub = object.__new__(BTAllianceMeshCoordinator)
    hub.protocol = Mock()
    hub._async_send_command = AsyncMock(return_value=True)
    assert await hub.async_set_brightness(9, 64)
    factory = hub._async_send_command.call_args.args[1]
    factory()
    hub.protocol.generate_luminance_command.assert_called_once_with(25)


@pytest.mark.asyncio
async def test_turn_on_failure_is_not_followed_by_brightness():
    hub = object.__new__(BTAllianceMeshCoordinator)
    hub.protocol = Mock()
    hub._async_send_command = AsyncMock(return_value=False)
    assert not await hub.async_turn_on(9)
    assert hub._async_send_command.await_count == 1
