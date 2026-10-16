"""Advertisement-only network observations, with no device connection path."""

from __future__ import annotations

import logging
import time
from copy import deepcopy
from datetime import timedelta
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import DOMAIN, CONF_MESH_NAME
from .device_types import device_type_name
from .discovery import advertisement

RECENT_WINDOW = 300
_LOGGER = logging.getLogger(__name__)


class PassiveNetworkMonitor(DataUpdateCoordinator):
    """Retain public advertisements without authentication, queries, or controls."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=f'{DOMAIN}_passive',
                         update_interval=timedelta(seconds=30))
        self.entry = entry
        self.mesh_name = entry.options.get(CONF_MESH_NAME, entry.data[CONF_MESH_NAME])
        self.devices: dict[str, dict[str, Any]] = {}
        self._store = Store(hass, 1, f'{DOMAIN}.passive.{entry.entry_id}')
        self._unsub_bluetooth = None

    async def async_start(self) -> None:
        saved = await self._store.async_load()
        if saved and saved.get('mesh_name') == self.mesh_name:
            self.devices = saved.get('devices', {})
        self._unsub_bluetooth = bluetooth.async_register_callback(
            self.hass, self._async_advertisement, {'local_name': self.mesh_name, 'connectable': False},
            bluetooth.BluetoothScanningMode.PASSIVE)
        self.async_set_updated_data(self.snapshot())

    @callback
    def _async_advertisement(self, info, change) -> None:
        if self._record_advertisement(info):
            self._store.async_delay_save(self._stored_data, 60)
            self.async_set_updated_data(self.snapshot())

    def _record_advertisement(self, info) -> bool:
        try:
            node = advertisement(info)
        except (ValueError, TypeError):
            return False
        if not node or node.get('mesh_name') != self.mesh_name:
            return False
        # HA replays cached advertisements. Preserve their age rather than marking them fresh.
        observed = getattr(info, 'time', 0)
        if observed <= 0:
            return False
        now = time.time()
        seen = now - max(0, time.monotonic() - observed)
        mac = node['mac_address']
        saved = self.devices.get(mac, {})
        if seen <= saved.get('last_seen', 0):
            return False
        record = {**saved, **node, 'last_seen': seen, 'first_seen': saved.get('first_seen', seen),
                  'source': getattr(info, 'source', None)}
        if node.get('mesh_address') is not None:
            record['mesh_address_observed_at'] = seen
        self.devices[mac] = record
        return True

    @callback
    def _stored_data(self) -> dict:
        return {'mesh_name': self.mesh_name, 'devices': deepcopy(self.devices)}

    @callback
    def snapshot(self) -> dict:
        now = time.time()
        devices = []
        for mac, saved in sorted(self.devices.items()):
            record = deepcopy(saved)
            record['device_type_name'] = device_type_name(record.get('device_type'))
            record['recently_seen'] = 0 <= now - record['last_seen'] <= RECENT_WINDOW
            for key in ('last_seen', 'first_seen', 'mesh_address_observed_at'):
                if key in record:
                    record[key] = dt_util.utc_from_timestamp(record[key]).isoformat()
            devices.append(record)
        return {'mesh_name': self.mesh_name, 'passive_only': True,
                'recent_window_seconds': RECENT_WINDOW, 'device_count': len(devices),
                'recent_device_count': sum(device['recently_seen'] for device in devices),
                'observed_devices': devices}

    async def _async_update_data(self) -> dict:
        # Consult received data only: identical advertisements can be callback-deduplicated.
        changed = False
        for info in bluetooth.async_discovered_service_info(self.hass, connectable=False):
            changed = self._record_advertisement(info) or changed
        if changed:
            self._store.async_delay_save(self._stored_data, 60)
        return self.snapshot()

    async def async_stop(self) -> None:
        if self._unsub_bluetooth:
            self._unsub_bluetooth()
            self._unsub_bluetooth = None
        await self._store.async_save(self._stored_data())
