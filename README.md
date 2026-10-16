# BTAlliance Mesh Lights

A local Home Assistant integration for compatible Alliance Outdoor Lighting,
BTAlliance, and Fulife Telink BLE Mesh lights and relays.

[![GitHub Release](https://img.shields.io/github/v/release/archiconda1976/BTAlliance)](https://github.com/archiconda1976/BTAlliance/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

[Latest release and release notes](https://github.com/archiconda1976/BTAlliance/releases/latest).
This README follows the repository code; check release notes for availability in
your installed version. HACS displays release versions when installing a
published release rather than a repository branch or commit.

## Features

- Individual lights, an **All Lights** broadcast control, and native mesh groups and control.
- On/off, brightness, RGB color, and color temperature controls.
- Multiple networks, discovered-network selection, and automatic gateway fallback.
- Preferred relay gateways, infrastructure diagnostics, and signal-strength data.
- Automatic discovery or explicit address approval, persistent caches, and exclusions.
- Legacy Android app settings import with device names and group definitions.
- Firmware/type/MAC attributes when available, without inventing unknown identities.
- Selected-device network moves and factory resets with explicit confirmation.
- Passive advertisement monitoring without connecting to or controlling devices.

## Requirements and Installation

- A recent Home Assistant Core installation. Current tests cover Core 2026.8 and
  2026.10; older versions have not been validated against all current features.
- HA Bluetooth access through a local adapter or configured ESPHome Bluetooth
  proxy. The proxy must have an available connection slot for active hubs.
- Powered, reachable lights and the correct mesh name/password for active control.

### HACS

1. In HACS, open **Custom repositories**.
2. Add `https://github.com/archiconda1976/BTAlliance` as an **Integration**.
3. Find BTAlliance/Alliance Outdoor Lights and download the desired release.
4. Restart Home Assistant, then add **Alliance Outdoor Lights** under
   **Settings > Devices & services > Add integration**.

### Manual Installation

Copy `custom_components/btalliance` into HA's `config/custom_components/`
directory, restart HA, and add the integration. Upgrade by replacing the
integration files and restarting HA. Back up your HA configuration first.

## Set Up a Network

1. Select an advertised network or enter its name manually. The factory default
   is `Fulife`; the usual Alliance password is `2846`. Use the actual credentials
   for your installation, not an assumed password for an unfamiliar network.
2. Choose **Automatic**, **Approval required**, or **Passive monitor**.
3. For an active hub, select a reachable gateway. Known relays are marked as
   preferred; compatible lights can also act as gateways.
4. Allow the gateway to connect and authenticate, then watch discovery populate
   the hub. Relayed status can take longer than direct advertisements.

Network names accept 1-10 UTF-8 bytes; passwords accept 1-16 bytes. Configure one
entry per network. A network already configured cannot be added a second time.
Changing the hub's configured name/password changes which network HA connects
to; it does not reprogram physical devices.

### Discovery Modes

| Mode | Behavior |
|---|---|
| Automatic | Responding lights are added and retained across restarts. |
| Approval required | Only approved mesh addresses become light entities; other observations remain diagnostic. |
| Passive monitor | Records public advertisements only; no login, queries, or lighting commands. |

Automatic discovery confirms a light from a valid full status response or two
online reports with different counters. Offline reports alone do not create a
light. A direct MAC mapping is not required for relayed light status. Known
relays become infrastructure diagnostic entities instead of light controls.

Use **Configure > Network Settings and Exclusions** to change discovery mode,
approved light addresses, mesh-address exclusions, or MAC exclusions. Addresses
are comma-separated integers from 1 through 254. The approved-address list does
not restrict automatic discovery. A matching address is not proof of ownership.

### Gateway Selection and Diagnostics

The configured gateway has first priority. Fallback prefers known relays, then
stronger signals, while checking network identity, exclusions, and recent
failures. Discovery lists type/model, MAC, signal strength, and mesh address when
advertised. A relay must belong to the same network as the lights it serves.

The hub's **Gateway Connection** diagnostic reports connected/logged-in state,
configured and selected gateway MACs, selected gateway RSSI, candidate failures,
discovered networks, observed mesh addresses, exclusions, and group metadata.
An absent configured gateway does not prevent trying other eligible candidates.
Connection failures use backoff; a candidate with session-setup failures may be
skipped for up to 15 minutes. The phone app or another HA instance holding a
Bluetooth connection can delay gateway access.

## Operate Lights and the Hub

Use individual light entities with normal HA light controls, scenes, and
automations. The **All Lights** entity supports the same lighting operations
using the mesh broadcast address `0xFFFF`. The hub also provides:

- **Refresh Mesh Status**: request status from the network.
- **Sync Mesh Time**: send the current date/time to the mesh.

A plain turn-on sends only the on command. It does not force brightness to 100%
or apply a shared brightness to group/broadcast members. The device retains or
restores its own output level. To choose a brightness explicitly, use the slider
or supply `brightness`/`brightness_pct` to HA's `light.turn_on` action. Unknown
levels are not replaced with a guessed value.

Color temperature controls cover 2700-6500 K. Brightness uses HA's 0-255 action
scale or its percentage UI. RGB channels use 0-255. Individual status comes from
received reports. All Lights retains some last-requested broadcast attributes
and can sample a representative known light; it does not verify that every light
matches that value. Sparse communication can leave attributes unknown or stale.

Lighting controls currently use five transmissions spaced about 350 ms apart.
Status and firmware queries use one transmission and are spaced at least one
second apart. These timings are fixed, not exposed as user options. A successful
Bluetooth write is not an end-device delivery acknowledgment.

**Broadcasts reach every compatible device sharing the network credentials,
including HA-excluded devices and potentially neighboring installations.**
HA exclusions and groups are not network-security boundaries.

## Import Legacy Android App Settings

1. On an active hub, open **Configure > Import Legacy App Settings** and submit
   to start a five-minute listening window. No phone IP, host IP, or subnet
   fields are required; enabled HA network interfaces are detected automatically.
2. In the Android app's **Share Settings**, select `Home Assistant <network>`
   and confirm the transfer. Keep both devices on the same trusted LAN.
3. In HA, review the received devices and groups. Select only devices you own;
   older phone inventories can contain stale or neighboring entries.
4. Check **Confirm Selected Import** and submit to apply the selection.

**Phone success means delivered, not imported.** HA advances the waiting form
to review and creates a **Settings Ready to Review** notification. Reopen the
importer to resume an unconfirmed review without resending. Sanitized reviews
survive hub reloads and HA restarts for 24 hours. Stop cancels a listening window;
Discard removes a pending review. Closing the dialog alone does not immediately
close the timed socket. The socket closes after one accepted transfer or timeout.

Import applies selected names and group definitions. Existing HA custom names
and entity IDs are preserved. Selected lights are enrolled once at confirmation;
selected relays become infrastructure diagnostics. Old imported records remain
descriptive metadata and cannot grant enrollment again at startup. Existing
persisted enrollment is preserved when upgrading from the earlier import behavior.
Importing a group does not implicitly enroll every member: also select the
devices you want to manage. Unselected prior import metadata is not deleted.

Imported memberships are labeled as app-provided hints until confirmed by a
device response; known device-reported membership takes precedence. Imported
MAC/type hints are separate from identities learned over Bluetooth. Import does
not send lighting controls, move networks, or program group memberships. The hub
reloads afterward and resumes ordinary discovery/status queries.

The legacy sharing protocol is **unencrypted and unauthenticated**. Any phone on
an enabled local private IPv4 network can send a matching-network export during
the window. Review before confirming. The import discards exported credentials
and raw transfer documents; only validated device/group fields are saved. The
active hub's separately configured login remains part of its HA configuration.

### Sharing Network Access

The receiver requires inbound **UDP 58024**. The phone receives advertisements
and acknowledgments on **UDP 58025**. The Android app also pings the receiver
before transmission, with a long timeout: allow **ICMPv4 echo requests from the
local LAN** or discovery may work while transfer appears stuck. Keep firewall
exceptions limited to your trusted LAN. The integration does not change firewall
rules or Docker port mappings automatically.

An ESPHome Bluetooth proxy does not forward this LAN sharing traffic. Passive
monitor entries do not provide an importer. Native host networking avoids many
container broadcast/NAT issues where the host platform supports it.

## Remove, Rediscover, or Exclude a Light

- **Remove** a removable light entity/device in HA to forget its current cache
  and enrollment. Disable is different: it leaves the registry entry configured.
  If HA offers only Disable, use the cleanup action or approval/exclusion settings.
- **Automatic discovery** can add a removed light again after a new valid device
  report. Imported metadata alone never recreates it. Its imported name is reused
  if the same address is discovered again on the same network.
- **Approval required** keeps a removed/revoked address out until you approve or
  explicitly import it again. Remaining old app records do not override revocation.
- **Exclude** an address to prevent rediscovery in either active discovery mode.
  MAC exclusions also prevent that physical device being used as a gateway.

Use **Cleanup Unvalidated Lights** in options, or the
`btalliance.cleanup_discovered_lights` action, to purge cached nodes. The action
retains `keep_addresses` plus validated lights by default. With
`require_validation: false`, it also retains currently discovered lights.
Wait for connection and discovery before cleanup, or explicitly keep known weak
lights so an absent reply does not cause unwanted removal. Cleanup also removes
approval for purged lights; imported names remain available for future discovery.

MAC-based light filtering needs a known mesh-address mapping; use address
exclusions if that mapping is unavailable. HA exclusion does not stop a relay's
physical forwarding or protect a light from a true network broadcast.

## Native Mesh Groups

Open **Configure > Manage Mesh Groups** to create/rename a local group control,
read an enrolled light's memberships, or add/remove a light. Short group
addresses **32769-33022** are supported.  Group addresses are not network names. 
Creating or renaming an HA control does not program physical memberships or rename 
a group in the phone app.

Add/remove operations require a light address, group address, confirmation, and
a readback. If an operation is unconfirmed, it may already have changed the
device: read memberships before retrying. Known address collisions are blocked;
unknown duplicates on shared networks remain possible.

Discovered/imported groups get their own light entities under the hub. They send
native group-addressed commands, not individual command fan-out or whole-network
broadcasts. Empty groups without an eligible enrolled member are unavailable and
cannot send lighting commands. State is aggregated from fresh member reports;
mixed or incomplete reports do not establish one uniform brightness or color.

## Device Identity and Passive Monitoring

Lights expose mesh address/network, `online`, firmware fields, and identity
attributes when known. `device_type`, `device_type_hex`, `device_type_name`, and
`device_type_source` describe learned types; examples are 40968 (`BL200-bt`) and
40979 (`Relay`). Unknown types keep their number without an invented model name.
Known model and Bluetooth MAC information also appears in HA Device info.

`mac_addresses` lists advertised matches. `mac_address` is populated only for a
single match; `mac_address_status` is `matched`, `not_found`, or `ambiguous`.
Known collisions suppress a single MAC/model identity. A short advertisement can
identify a type without revealing its mesh address. Relayed status and firmware
queries do not establish a MAC mapping. Learned identity is cached per network;
imported hints use separate `app_import_*` attributes.

Firmware-name queries run during discovery. Retry with
`btalliance.query_device_firmware`; read a directly reachable device's standard
firmware revision with `btalliance.refresh_device_metadata`. Unknown fields are
not proof of device failure. Neither firmware operation enrolls a light.

Passive monitoring creates only a **Recent Advertisements** diagnostic binary
sensor. Attributes retain observed MACs, type, advertised mesh address when
available, RSSI, first/last seen, and recent-device counts. Recent presence means
an advertisement within five minutes; historical sightings survive restarts.
Cached advertisements retain their original timestamps. Another integration may
independently request active scanning, but this monitor never connects, logs in,
queries, or controls devices. Recreate an entry to switch between passive and
active operation. Sightings never establish ownership or authorize network moves.

## EXPERIMENTAL - Move or Reset a Selected Device

Use **Configure > Manage a Selected Device**. Select a Bluetooth MAC you have
confirmed is yours, choose firmware inspection, network move, or factory reset,
and confirm the operation. The device must be directly reachable via HA
Bluetooth or an ESPHome proxy and advertise the current hub's network.

A move writes only that device's network credentials using its current login.
It preserves the mesh address; it does not move a group, renumber collisions,
move other relays/lights, or update the hub credentials automatically. Check the
destination network for duplicate addresses and configure/select its hub afterward.

Factory reset requires current credentials, returns the selected device toward
the factory network, and may clear other settings. Pairing confirmation is not
proof of reconnection or of every firmware-specific reset effect. Failed or
uncertain moves/resets are not automatically retried. Inspect the device in the
phone app before repeating an operation.

The hub gateway is paused during direct-device management and reconnects through
normal operation. A departing device is removed from the light cache only when
its physical identity is unambiguous. Secure networks provide separation from
devices with other credentials; groups and HA exclusions do not.

## Developer Tools Actions

All actions below use the `btalliance.` prefix. Supply `entry_id` when multiple
active hubs exist. Passive entries cannot send commands. Group/device management
actions support optional responses describing confirmation or uncertainty.

| Action | Additional fields / purpose |
|---|---|
| `broadcast_turn_on`, `broadcast_turn_off` | Network-wide on/off; on does not force brightness. |
| `broadcast_set_brightness` | `brightness`: 0-255. |
| `broadcast_set_rgb` | `red`, `green`, `blue`: 0-255 each. |
| `broadcast_set_color_temp` | `color_temp_pct`: 0 warm to 100 cool. |
| `broadcast_query_status`, `broadcast_sync_time` | Request reports / synchronize mesh time. |
| `cleanup_discovered_lights` | Optional comma-separated `keep_addresses`; `require_validation` defaults to true. |
| `configure_group` | `group_address`, `name`; local control only. |
| `query_light_groups` | `mesh_address`; read memberships. |
| `add_light_to_group`, `remove_light_from_group` | `mesh_address`, `group_address`, `confirm: true`. |
| `query_device_firmware` | `mesh_address`; request firmware name over the mesh. |
| `refresh_device_metadata` | `mac_address`; direct firmware-revision read. |
| EXPERIMENTAL `move_device_to_network` | `mac_address`, `new_mesh_name`, optional `new_password`, `confirm: true`. |
| EXPERIMENTAL `reset_device_to_factory` | `mac_address`, `confirm: true`; destructive operation. |

For routine entity lighting, use HA's standard `light.turn_on` / `light.turn_off`
actions instead of supplying mesh addresses manually. Names may change on import;
existing entity IDs remain stable for automations.

## Troubleshooting

| Symptom | Checks |
|---|---|
| Gateway disconnected | Verify power, proxy availability, credentials, and competing app/HA connections; inspect candidate failures/backoff. |
| Missing light | Check range/relays, discovery mode, approvals, exclusions, and hub observed addresses. Weak devices may need more time. |
| Unwanted or duplicate lights | Shared credentials may expose neighboring devices. Exclude known addresses, use approval mode, and plan a separate network. |
| Missing MAC/type/firmware | These fields need suitable direct advertisements or replies; relayed status alone is insufficient. |
| Group unknown/mixed state | Refresh member reports and query memberships; do not assume a command reached every member. |
| Receiver visible but transfer stalls | Check LAN ping access, UDP 58024/58025, container forwarding, and the helper's status/rejection metadata. |
| Phone says success but names unchanged | Finish the HA review and confirmation; use its receipt notification or reopen the importer. |
| Removed light returns | A new report can rediscover it in automatic mode. Exclude it for persistent suppression; old imports cannot reenroll it at startup. |

For debug logs, enable `custom_components.btalliance` at debug level temporarily.
Review logs before sharing: they may contain device addresses and packet/status
details. Avoid repeatedly issuing network-change/reset actions while diagnosing
uncertain outcomes.

## Development

Runtime files are under `custom_components/btalliance/`; regression tests are in
`tests/`, and the optional host sharing helper is in `tools/`. Tests use real HA
schemas with mocked Bluetooth transport and require HA's Python dependencies:

```text
python -m pytest -q tests
```

Tests do not operate physical lights. Integration startup in a real HA instance
does perform normal discovery/status queries. Encryption/session handling,
on/off, status, groups, and device provisioning use the compatible Telink mesh
protocol. Service UUID: `00010203-0405-0607-0809-0a0b0c0d1910`.

## License and Disclaimer

This project uses the MIT license. BTAlliance/Fulife and other product names are
trademarks of their respective owners. This is an unofficial integration and is
not affiliated with or endorsed by the device manufacturers.
