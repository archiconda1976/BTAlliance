"""Passive network observations must never become device connections or commands."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.btalliance import _get_coordinator, async_setup_entry, async_unload_entry
from custom_components.btalliance import config_flow, discovery
from custom_components.btalliance.passive import PassiveNetworkMonitor
from custom_components.btalliance.binary_sensor import BTAlliancePassiveNetwork


@pytest_asyncio.fixture
async def monitor(tmp_path, monkeypatch):
    instance = PassiveNetworkMonitor(HomeAssistant(str(tmp_path)), SimpleNamespace(
        entry_id='passive', data={'mesh_name': 'Kelly', 'discovery_mode': 'passive'},
        options={}, async_on_unload=Mock()))
    instance._store = SimpleNamespace(async_load=AsyncMock(return_value=None),
        async_save=AsyncMock(), async_delay_save=Mock())
    monkeypatch.setattr('custom_components.btalliance.passive.time',
                        SimpleNamespace(time=lambda: 1000, monotonic=lambda: 100))
    return instance


def info(network='Kelly', observed=100, extended=True):
    return SimpleNamespace(address='FF:00:05:06:1B:2E', name=network, rssi=-99, time=observed,
        source='proxy', manufacturer_data={529: bytes.fromhex('a0132e1b06050400010400') if extended
                                         else bytes.fromhex('a0132e1b0605')}, service_uuids=[])


@pytest.mark.asyncio
async def test_passive_callback_registration_and_history_restore(monitor, monkeypatch):
    monitor._store.async_load.return_value = {'mesh_name': 'Kelly', 'devices': {
        'old': {'mac_address': 'old', 'last_seen': 500, 'first_seen': 500}}}
    unregister = Mock()
    register = Mock(return_value=unregister)
    monkeypatch.setattr('custom_components.btalliance.passive.bluetooth.async_register_callback', register)
    await monitor.async_start()
    assert register.call_args.args[2] == {'local_name': 'Kelly', 'connectable': False}
    assert register.call_args.args[3].value == 'passive'
    assert monitor.snapshot()['recent_device_count'] == 0
    assert monitor.snapshot()['device_count'] == 1
    await monitor.async_stop()
    unregister.assert_called_once()
    monitor._store.async_save.assert_awaited_once()


def test_observes_only_target_network_and_does_not_invent_identity(monitor):
    monitor._async_advertisement(info('Archibald'), None)
    assert not monitor.devices
    monitor._async_advertisement(info(extended=False), None)
    result = monitor.snapshot()
    assert result['recent_device_count'] == 1
    device = result['observed_devices'][0]
    assert device['device_type_name'] == 'Relay'
    assert 'mesh_address' not in device
    assert device['mac_address'] == 'FF:00:05:06:1B:2E'
    assert result['passive_only'] is True


def test_cached_advertisement_replay_is_not_fresh(monitor):
    monitor._async_advertisement(info(observed=1), None)
    assert monitor.devices[info().address]['last_seen'] == 901
    # A much older restored record must stay old when no new advertisement exists.
    monitor.devices[info().address]['last_seen'] = 500
    monitor.devices[info().address]['first_seen'] = 500
    assert monitor.snapshot()['recent_device_count'] == 0


def test_older_replay_does_not_overwrite_newer_metadata(monitor):
    monitor._async_advertisement(info(), None)
    monitor._async_advertisement(info(observed=50, extended=False), None)
    assert monitor.devices[info().address]['last_seen'] == 1000
    assert monitor.devices[info().address]['mesh_address'] == 4


def test_presence_expires_but_history_remains(monitor, monkeypatch):
    monitor._async_advertisement(info(), None)
    entity = BTAlliancePassiveNetwork(monitor, 'passive')
    assert entity.is_on is True
    monkeypatch.setattr('custom_components.btalliance.passive.time.time', lambda: 1301)
    assert entity.is_on is False
    assert entity.extra_state_attributes['device_count'] == 1
    assert entity.extra_state_attributes['observed_devices'][0]['recently_seen'] is False


@pytest.mark.asyncio
async def test_periodic_refresh_reads_received_data_without_requesting_scan(monitor, monkeypatch):
    received = Mock(return_value=[info(), info('Archibald')])
    monkeypatch.setattr('custom_components.btalliance.passive.bluetooth.async_discovered_service_info', received)
    result = await monitor._async_update_data()
    received.assert_called_once_with(monitor.hass, connectable=False)
    assert result['device_count'] == 1
    assert result['recent_device_count'] == 1


@pytest.mark.asyncio
async def test_passive_setup_without_visible_devices_or_credentials(monkeypatch, tmp_path):
    flow = config_flow.BTAllianceConfigFlow()
    flow.hass = HomeAssistant(str(tmp_path))
    flow.context = {}
    monkeypatch.setattr(flow, '_async_current_entries', lambda: [])
    monkeypatch.setattr(flow, 'async_set_unique_id', AsyncMock())
    monkeypatch.setattr(flow, '_abort_if_unique_id_configured', Mock())
    monkeypatch.setattr(discovery.bluetooth, 'async_discovered_service_info', lambda _: [])
    result = await flow.async_step_user({'mesh_name': 'Kelly', 'discovery_mode': 'passive'})
    assert result['type'] == 'create_entry'
    assert result['data'] == {'mesh_name': 'Kelly', 'discovery_mode': 'passive'}
    assert result['title'] == 'Kelly Passive Monitor'


@pytest.mark.asyncio
async def test_passive_entry_never_constructs_active_coordinator(monkeypatch, monitor):
    fake = SimpleNamespace(data={}, config_entries=SimpleNamespace(async_forward_entry_setups=AsyncMock()))
    entry = SimpleNamespace(entry_id='passive', data={'mesh_name': 'Kelly', 'discovery_mode': 'passive'},
        options={}, async_on_unload=Mock(), add_update_listener=Mock())
    factory = Mock(side_effect=AssertionError('Active coordinator must not be constructed'))
    monkeypatch.setattr('custom_components.btalliance.BTAllianceMeshCoordinator', factory)
    monkeypatch.setattr('custom_components.btalliance.PassiveNetworkMonitor', Mock(return_value=monitor))
    monitor.async_start = AsyncMock()
    assert await async_setup_entry(fake, entry)
    fake.config_entries.async_forward_entry_setups.assert_awaited_once_with(entry, [Platform.BINARY_SENSOR])
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_device_commands_cannot_target_passive_monitor(monitor):
    active = SimpleNamespace(mesh_name='Archibald')
    fake = SimpleNamespace(data={'btalliance': {'passive': monitor, 'active': active}})
    with pytest.raises(HomeAssistantError, match='Passive monitors cannot'):
        await _get_coordinator(fake, SimpleNamespace(data={'entry_id': 'passive'}))
    assert await _get_coordinator(fake, SimpleNamespace(data={})) is active
    fake.data['btalliance'].pop('active')
    with pytest.raises(HomeAssistantError, match='No active'):
        await _get_coordinator(fake, SimpleNamespace(data={}))


@pytest.mark.asyncio
async def test_passive_unload_only_unloads_diagnostics(monitor):
    monitor.async_stop = AsyncMock()
    fake = SimpleNamespace(data={'btalliance': {'passive': monitor}}, config_entries=SimpleNamespace(
        async_unload_platforms=AsyncMock(return_value=True)))
    entry = SimpleNamespace(entry_id='passive', data={'discovery_mode': 'passive'})
    assert await async_unload_entry(fake, entry)
    fake.config_entries.async_unload_platforms.assert_awaited_once_with(entry, [Platform.BINARY_SENSOR])
    monitor.async_stop.assert_awaited_once()
