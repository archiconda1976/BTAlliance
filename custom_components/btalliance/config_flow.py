"""Network-aware setup and selected-device management for BTAlliance."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector

from .const import (
    DOMAIN, DEFAULT_MESH_NAME, DEFAULT_PASSWORD, CONF_MESH_NAME, CONF_PASSWORD,
    CONF_GATEWAY_ADDRESS, CONF_INFRASTRUCTURE_MESH_ADDRESSES,
    CONF_DISCOVERED_LIGHT_MESH_ADDRESSES, CONF_CLEANUP_UNVALIDATED_LIGHTS,
    CONF_EXCLUDED_MESH_ADDRESSES, CONF_EXCLUDED_MAC_ADDRESSES,
    CONF_ENROLLED_MESH_ADDRESSES,
    CONF_DISCOVERY_MODE, DISCOVERY_AUTOMATIC, DISCOVERY_APPROVAL, DISCOVERY_PASSIVE,
)
from .discovery import (
    advertisement, discovered_devices, discovered_networks, normalize_mac,
    parse_addresses, parse_macs, validate_network_name, validate_password,
    is_relay_device,
)
from .device_types import device_type_name
from .groups import validate_group_address
from .share_settings import (IMPORT_KEY, LISTENER_KEY, ShareSettingsListener, select_import,
                             register_listener, restore_pending_import, import_enrollment_options)
from .const import CONF_MESH_GROUPS


def network_selector(hass, default: str = DEFAULT_MESH_NAME):
    """Offer live network names while retaining manual entry."""
    return selector.SelectSelector(selector.SelectSelectorConfig(
        options=sorted(set(discovered_networks(hass)) | {default}), custom_value=True,
        mode=selector.SelectSelectorMode.DROPDOWN,
    ))


def credentials_schema(hass, defaults: dict[str, Any], *, allow_passive: bool = True) -> dict:
    return {
        vol.Required(CONF_MESH_NAME, default=defaults.get(CONF_MESH_NAME, DEFAULT_MESH_NAME)):
            network_selector(hass, defaults.get(CONF_MESH_NAME, DEFAULT_MESH_NAME)),
        vol.Optional(CONF_PASSWORD, default=defaults.get(CONF_PASSWORD, DEFAULT_PASSWORD)):
            selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)),
        vol.Required(CONF_DISCOVERY_MODE, default=defaults.get(CONF_DISCOVERY_MODE, DISCOVERY_AUTOMATIC)):
            selector.SelectSelector(selector.SelectSelectorConfig(options=[
                {'value': DISCOVERY_AUTOMATIC, 'label': 'Automatic'},
                {'value': DISCOVERY_APPROVAL, 'label': 'Approval required'},
            ] + ([{'value': DISCOVERY_PASSIVE, 'label': 'Passive monitor (no connection)'}]
                 if allow_passive else []), mode=selector.SelectSelectorMode.DROPDOWN)),
        vol.Optional(CONF_EXCLUDED_MESH_ADDRESSES, default=defaults.get(CONF_EXCLUDED_MESH_ADDRESSES, "")): str,
        vol.Optional(CONF_EXCLUDED_MAC_ADDRESSES, default=defaults.get(CONF_EXCLUDED_MAC_ADDRESSES, "")): str,
        vol.Optional(CONF_ENROLLED_MESH_ADDRESSES, default=defaults.get(CONF_ENROLLED_MESH_ADDRESSES, defaults.get(CONF_DISCOVERED_LIGHT_MESH_ADDRESSES, ""))): str,
    }


def validate_settings(data: dict[str, Any]) -> None:
    if data.get(CONF_DISCOVERY_MODE, DISCOVERY_AUTOMATIC) not in (DISCOVERY_AUTOMATIC, DISCOVERY_APPROVAL, DISCOVERY_PASSIVE):
        raise ValueError('Invalid discovery mode')
    validate_network_name(data[CONF_MESH_NAME])
    if data.get(CONF_DISCOVERY_MODE) != DISCOVERY_PASSIVE:
        validate_password(data.get(CONF_PASSWORD, DEFAULT_PASSWORD))
    parse_addresses(data.get(CONF_EXCLUDED_MESH_ADDRESSES, ""))
    parse_macs(data.get(CONF_EXCLUDED_MAC_ADDRESSES, ""))
    parse_addresses(data.get(CONF_INFRASTRUCTURE_MESH_ADDRESSES, ""))
    parse_addresses(data.get(CONF_ENROLLED_MESH_ADDRESSES, ""))


def is_fulife_device(info: BluetoothServiceInfoBleak) -> bool:
    return advertisement(info) is not None


def mac_to_int(mac: str) -> int:
    return int(normalize_mac(mac).replace(":", ""), 16)


class BTAllianceConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._setup_data: dict[str, Any] = {}
        self._discovered_device: BluetoothServiceInfoBleak | None = None

    def _network_configured(self, name: str) -> bool:
        return any(entry.options.get(CONF_MESH_NAME, entry.data.get(CONF_MESH_NAME)) == name
                   for entry in self._async_current_entries())

    async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfoBleak) -> FlowResult:
        node = advertisement(discovery_info)
        if not node or not node["mesh_name"]:
            return self.async_abort(reason="not_supported")
        if self._network_configured(node["mesh_name"]):
            return self.async_abort(reason="already_configured")
        await self.async_set_unique_id(node["mesh_name"])
        self._abort_if_unique_id_configured()
        self._discovered_device = discovery_info
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(self, user_input=None) -> FlowResult:
        if self._discovered_device is None:
            return self.async_abort(reason="not_supported")
        if user_input is not None:
            return await self.async_step_user(user_input)
        return self.async_show_form(
            step_id="bluetooth_confirm",
            data_schema=vol.Schema(credentials_schema(self.hass, {CONF_MESH_NAME: self._discovered_device.name})),
            description_placeholders={"name": self._discovered_device.name, "address": self._discovered_device.address},
        )

    async def async_step_user(self, user_input=None) -> FlowResult:
        errors = {}
        if user_input is not None:
            try:
                validate_settings(user_input)
            except ValueError:
                errors["base"] = "invalid_settings"
            else:
                if self._network_configured(user_input[CONF_MESH_NAME]):
                    return self.async_abort(reason="already_configured")
                if user_input.get(CONF_DISCOVERY_MODE) == DISCOVERY_PASSIVE:
                    name = user_input[CONF_MESH_NAME]
                    await self.async_set_unique_id(name)
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(title=f'{name} Passive Monitor', data={
                        CONF_MESH_NAME: name, CONF_DISCOVERY_MODE: DISCOVERY_PASSIVE})
                self._setup_data = dict(user_input)
                self._setup_data.setdefault(CONF_PASSWORD, DEFAULT_PASSWORD)
                return await self.async_step_pick_device()
        return self.async_show_form(step_id="user", errors=errors,
                                    data_schema=vol.Schema(credentials_schema(self.hass, user_input or {})))

    async def async_step_pick_device(self, user_input=None) -> FlowResult:
        name = self._setup_data[CONF_MESH_NAME]
        excluded = parse_macs(self._setup_data.get(CONF_EXCLUDED_MAC_ADDRESSES, ""))
        excluded_addresses = parse_addresses(self._setup_data.get(CONF_EXCLUDED_MESH_ADDRESSES, ""))
        devices = {mac: device for mac, device in discovered_devices(self.hass).items()
                   if device["mesh_name"] == name and mac not in excluded
                   and device.get("mesh_address") not in excluded_addresses}
        errors = {}
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            if address not in devices:
                errors["base"] = "device_unavailable"
            else:
                if self._network_configured(name):
                    return self.async_abort(reason="already_configured")
                await self.async_set_unique_id(name)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=f"{name} Mesh", data={
                    **self._setup_data, CONF_GATEWAY_ADDRESS: mac_to_int(address),
                })
        if not devices:
            return self.async_abort(reason="no_devices_found")
        options = []
        for mac, device in sorted(devices.items(), key=lambda item: (
                not is_relay_device(item[1]), -(item[1].get('rssi') or -999), item[0])):
            kind = 'Relay - preferred gateway' if is_relay_device(device) else device_type_name(device.get('device_type')) or 'Unknown type'
            address = f", mesh {device['mesh_address']}" if device.get('mesh_address') else ''
            options.append({'value': mac, 'label': f"{kind} ({mac}{address}, {device['rssi']} dBm)"})
        return self.async_show_form(step_id="pick_device", errors=errors, data_schema=vol.Schema({
            vol.Required(CONF_ADDRESS, default=options[0]['value']): selector.SelectSelector(selector.SelectSelectorConfig(options=options)),
        }))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return BTAllianceOptionsFlow()


class BTAllianceOptionsFlow(config_entries.OptionsFlow):
    def __init__(self) -> None:
        self._operation: dict[str, Any] = {}
        self._share_state_unsub = None
        self._share_watched_listener = None

    @callback
    def async_remove(self):
        if self._share_state_unsub is not None:
            self._share_state_unsub()
            self._share_state_unsub = None
        self._share_watched_listener = None
        super().async_remove()

    def _watch_share_listener(self, listener):
        if self._share_watched_listener is not listener:
            if self._share_state_unsub is not None:
                self._share_state_unsub()
            self._share_state_unsub = listener.add_state_callback(self.async_notify_flow_changed)
            self._share_watched_listener = listener

    async def async_step_init(self, user_input=None) -> FlowResult:
        if self.config_entry.data.get(CONF_DISCOVERY_MODE) == DISCOVERY_PASSIVE:
            return await self.async_step_passive_settings(user_input)
        return self.async_show_menu(step_id="init", menu_options=["settings", "manage_device", "manage_group", "share_settings"])

    def _share_listener(self):
        current = self.hass.data.get(LISTENER_KEY)
        if current is None:
            name = self.config_entry.options.get(CONF_MESH_NAME, self.config_entry.data.get(CONF_MESH_NAME))
            restore_pending_import(self.hass, self.config_entry, name)
            current = self.hass.data.get(LISTENER_KEY)
        listener = current[1] if current and current[0] == self.config_entry.entry_id else None
        if listener is not None:
            self._watch_share_listener(listener)
        return listener

    async def async_step_share_settings(self, user_input=None) -> FlowResult:
        listener = self._share_listener()
        if listener is not None:
            return await self.async_step_share_wait()
        errors = {}
        if user_input is not None:
            current = self.hass.data.get(LISTENER_KEY)
            if current and current[1].state != 'listening' and current[1].staged is None:
                self.hass.data.pop(LISTENER_KEY, None)
                current = None
            if current:
                errors['base'] = 'share_busy'
            else:
                try:
                    hub = self.hass.data[DOMAIN][self.config_entry.entry_id]
                    listener = await ShareSettingsListener.async_start_auto(
                        self.hass, hub.mesh_name, f'Home Assistant {hub.mesh_name}')
                except (ValueError, OSError):
                    errors['base'] = 'share_start_failed'
                else:
                    register_listener(self.hass, self.config_entry, listener)
                    self._watch_share_listener(listener)
                    return await self.async_step_share_wait()
        return self.async_show_form(step_id='share_settings', errors=errors, data_schema=vol.Schema({}))

    async def async_step_share_wait(self, user_input=None) -> FlowResult:
        listener = self._share_listener()
        if listener is None:
            return await self.async_step_share_settings()
        if user_input and user_input.get('stop'):
            listener.discard()
            self.hass.data.pop(LISTENER_KEY, None)
            return self.async_create_entry(title='', data=dict(self.config_entry.options))
        if listener.staged is not None:
            return await self.async_step_share_preview()
        if listener.state != 'listening':
            listener.discard()
            self.hass.data.pop(LISTENER_KEY, None)
            return self.async_abort(reason='share_timed_out')
        return self.async_show_form(step_id='share_wait',
            description_placeholders={'state': listener.state, 'rejected': str(listener.rejected)},
            data_schema=vol.Schema({vol.Required('stop', default=False): bool}))

    async def async_step_share_preview(self, user_input=None) -> FlowResult:
        listener = self._share_listener()
        if listener is None or listener.staged is None:
            return await self.async_step_share_settings()
        hub = self.hass.data[DOMAIN][self.config_entry.entry_id]
        staged = listener.staged
        errors = {}
        if user_input is not None:
            if user_input.get('discard'):
                listener.discard()
                self.hass.data.pop(LISTENER_KEY, None)
                return self.async_create_entry(title='', data=dict(self.config_entry.options))
            try:
                if not user_input.get('confirm'):
                    raise ValueError('Confirmation required')
                imported = select_import(staged, user_input.get('devices', []), user_input.get('groups', []), hub)
                enrollment_options = import_enrollment_options(hub, imported, user_input.get('devices', []))
            except (ValueError, KeyError):
                errors['base'] = 'share_invalid_selection'
            else:
                listener.discard()
                self.hass.data.pop(LISTENER_KEY, None)
                group_cache = self.config_entry.options.get(CONF_MESH_GROUPS, {})
                group_cache = dict(group_cache) if group_cache.get('mesh_name') == hub.mesh_name else {'mesh_name': hub.mesh_name}
                group_cache['groups'] = {**group_cache.get('groups', {}),
                    **{str(address): {'name': staged['groups'][str(address)]['name']}
                       for address in user_input.get('groups', [])}}
                return self.async_create_entry(title='', data={**self.config_entry.options,
                    **enrollment_options, IMPORT_KEY: imported, CONF_MESH_GROUPS: group_cache})
        devices = [{'value': address,
                    'label': f"{node['name']} ({address}, {device_type_name(node['device_type']) or 'Unknown type'})"}
                   for address, node in staged['devices'].items()]
        groups = [{'value': address, 'label': f"{group['name']} ({address}, {len(group['members'])} members)"}
                  for address, group in staged['groups'].items()]
        return self.async_show_form(step_id='share_preview', errors=errors,
            description_placeholders={'network': hub.mesh_name, 'devices': str(len(devices)), 'groups': str(len(groups))},
            data_schema=vol.Schema({
                vol.Optional('devices', default=[address for address in staged['devices']
                    if int(address) in hub.cached_light_mesh_addresses and not hub.is_excluded_address(int(address))]):
                    selector.SelectSelector(selector.SelectSelectorConfig(options=devices, multiple=True)),
                vol.Optional('groups', default=[]):
                    selector.SelectSelector(selector.SelectSelectorConfig(options=groups, multiple=True)),
                vol.Required('confirm', default=False): bool,
                vol.Required('discard', default=False): bool,
            }))

    async def async_step_passive_settings(self, user_input=None) -> FlowResult:
        errors = {}
        if user_input is not None:
            try:
                validate_network_name(user_input[CONF_MESH_NAME])
            except ValueError:
                errors['base'] = 'invalid_settings'
            else:
                if any(entry.entry_id != self.config_entry.entry_id and
                       entry.options.get(CONF_MESH_NAME, entry.data.get(CONF_MESH_NAME)) == user_input[CONF_MESH_NAME]
                       for entry in self.hass.config_entries.async_entries(DOMAIN)):
                    return self.async_abort(reason='already_configured')
                return self.async_create_entry(title='', data={**self.config_entry.options,
                    CONF_MESH_NAME: user_input[CONF_MESH_NAME]})
        name = self.config_entry.options.get(CONF_MESH_NAME, self.config_entry.data[CONF_MESH_NAME])
        return self.async_show_form(step_id='passive_settings', errors=errors, data_schema=vol.Schema({
            vol.Required(CONF_MESH_NAME, default=name): network_selector(self.hass, name)}))

    async def async_step_manage_group(self, user_input=None) -> FlowResult:
        coordinator = self.hass.data[DOMAIN][self.config_entry.entry_id]
        errors = {}
        if user_input is not None:
            try:
                address = validate_group_address(user_input['group_address'])
                operation = user_input['operation']
                if operation == 'configure':
                    coordinator.configure_group(address, user_input['name'])
                elif operation == 'query':
                    if await coordinator.async_query_groups(int(user_input['mesh_address'])) is None:
                        raise HomeAssistantError('No complete group membership reply')
                else:
                    if not user_input.get('confirm'):
                        raise ValueError('Confirmation required')
                    await coordinator.async_set_group_membership(int(user_input['mesh_address']), address, operation == 'add')
            except ValueError:
                errors['base'] = 'invalid_settings'
            except HomeAssistantError:
                errors['base'] = 'group_unconfirmed'
            else:
                return self.async_create_entry(title='', data=dict(self.config_entry.options))
        defaults = user_input or {}
        groups = [{'value': str(address), 'label': f"{group['name']} ({address})"}
                  for address, group in sorted(coordinator.mesh_groups.items())]
        return self.async_show_form(step_id='manage_group', errors=errors, data_schema=vol.Schema({
            vol.Required('operation', default=defaults.get('operation', 'query')):
                selector.SelectSelector(selector.SelectSelectorConfig(options=[
                    {'value': 'query', 'label': 'Read light memberships'},
                    {'value': 'configure', 'label': 'Create or rename group control'},
                    {'value': 'add', 'label': 'Add light to group'},
                    {'value': 'remove', 'label': 'Remove light from group'},
                ])),
            vol.Required('group_address', default=str(defaults.get('group_address', 32785))):
                selector.SelectSelector(selector.SelectSelectorConfig(options=groups, custom_value=True)),
            vol.Optional('name', default=defaults.get('name', 'Group 17')): str,
            vol.Optional('mesh_address', default=str(defaults.get('mesh_address', min(coordinator.enrolled_mesh_addresses, default=1)))):
                selector.SelectSelector(selector.SelectSelectorConfig(options=[
                    {'value': str(address), 'label': f'Light {address}'}
                    for address in sorted(coordinator.enrolled_mesh_addresses)
                    if coordinator._is_controllable_light_address(address)])),
            vol.Required('confirm', default=False): bool,
        }))

    async def async_step_settings(self, user_input=None) -> FlowResult:
        errors = {}
        defaults = {**self.config_entry.data, **self.config_entry.options}
        if user_input is not None:
            try:
                validate_settings(user_input)
            except ValueError:
                errors["base"] = "invalid_settings"
            else:
                new_name = user_input[CONF_MESH_NAME]
                if user_input.get(CONF_DISCOVERY_MODE) == DISCOVERY_PASSIVE:
                    errors['base'] = 'invalid_settings'
                    return self.async_show_form(step_id='settings', errors=errors,
                        data_schema=vol.Schema(credentials_schema(self.hass, defaults, allow_passive=False)))
                if any(entry.entry_id != self.config_entry.entry_id and
                       entry.options.get(CONF_MESH_NAME, entry.data.get(CONF_MESH_NAME)) == new_name
                       for entry in self.hass.config_entries.async_entries(DOMAIN)):
                    return self.async_abort(reason="already_configured")
                options = {**self.config_entry.options, **user_input}
                cleanup = options.pop(CONF_CLEANUP_UNVALIDATED_LIGHTS, False)
                coordinator = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
                if coordinator is not None:
                    if new_name != coordinator.mesh_name:
                        for address in list(coordinator.cached_light_mesh_addresses):
                            coordinator.remove_cached_light_address(address)
                            coordinator._remove_light_entity_and_device(address)
                    coordinator.apply_exclusions(parse_addresses(user_input.get(CONF_EXCLUDED_MESH_ADDRESSES, "")),
                                                 parse_macs(user_input.get(CONF_EXCLUDED_MAC_ADDRESSES, "")))
                    coordinator.discovery_mode = user_input.get(CONF_DISCOVERY_MODE, DISCOVERY_AUTOMATIC)
                    if coordinator.discovery_mode == DISCOVERY_APPROVAL and CONF_ENROLLED_MESH_ADDRESSES in user_input:
                        coordinator.apply_enrollment(parse_addresses(user_input[CONF_ENROLLED_MESH_ADDRESSES]))
                    if cleanup:
                        coordinator.cleanup_cached_light_addresses()
                    options[CONF_DISCOVERED_LIGHT_MESH_ADDRESSES] = ",".join(map(str, sorted(coordinator.cached_light_mesh_addresses)))
                return self.async_create_entry(title="", data=options)
            defaults = {**defaults, **user_input}
        schema = credentials_schema(self.hass, defaults, allow_passive=False)
        schema[vol.Optional(CONF_INFRASTRUCTURE_MESH_ADDRESSES, default=defaults.get(CONF_INFRASTRUCTURE_MESH_ADDRESSES, ""))] = str
        schema[vol.Optional(CONF_CLEANUP_UNVALIDATED_LIGHTS, default=False)] = bool
        return self.async_show_form(step_id="settings", errors=errors, data_schema=vol.Schema(schema))

    async def async_step_manage_device(self, user_input=None) -> FlowResult:
        coordinator = self.hass.data[DOMAIN][self.config_entry.entry_id]
        devices = {mac: node for mac, node in discovered_devices(self.hass).items() if node["mesh_name"] == coordinator.mesh_name}
        errors = {}
        if user_input is not None:
            try:
                if user_input["mac_address"] not in devices:
                    raise ValueError("Device unavailable")
                if user_input["operation"] == "move":
                    validate_network_name(user_input["new_mesh_name"])
                    validate_password(user_input["new_password"])
            except ValueError:
                errors["base"] = "invalid_settings"
            else:
                self._operation = dict(user_input)
                return await self.async_step_confirm_device()
        if not devices:
            return self.async_abort(reason="no_devices_found")
        options = [{"value": mac, "label": f"{mac} - address {node.get('mesh_address', 'unknown')} ({node['rssi']} dBm)"}
                   for mac, node in sorted(devices.items())]
        return self.async_show_form(step_id="manage_device", errors=errors, data_schema=vol.Schema({
            vol.Required("mac_address"): selector.SelectSelector(selector.SelectSelectorConfig(options=options)),
            vol.Required("operation", default="inspect"): selector.SelectSelector(selector.SelectSelectorConfig(options=[
                {"value": "inspect", "label": "Read firmware revision"},
                {"value": "move", "label": "Move to another network"},
                {"value": "reset", "label": "Reset to factory network"},
            ])),
            vol.Optional("new_mesh_name", default=DEFAULT_MESH_NAME): network_selector(self.hass),
            vol.Optional("new_password", default=DEFAULT_PASSWORD): selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)),
        }))

    async def async_step_confirm_device(self, user_input=None) -> FlowResult:
        errors = {}
        if user_input is not None and user_input.get("confirm"):
            coordinator = self.hass.data[DOMAIN][self.config_entry.entry_id]
            try:
                await coordinator.async_manage_device(self._operation["mac_address"], self._operation["operation"],
                                                      self._operation.get("new_mesh_name"), self._operation.get("new_password", DEFAULT_PASSWORD))
            except (HomeAssistantError, ValueError):
                errors["base"] = "operation_failed"
            else:
                return self.async_create_entry(title="", data=dict(self.config_entry.options))
        return self.async_show_form(step_id="confirm_device", errors=errors,
                                    description_placeholders={"address": self._operation["mac_address"], "operation": self._operation["operation"]},
                                    data_schema=vol.Schema({vol.Required("confirm", default=False): bool}))
