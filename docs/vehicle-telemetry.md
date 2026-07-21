# StarPilot EV Vehicle Telemetry

StarPilot uses the fork-neutral [`system.vehicle_telemetry`](vehicle-telemetry-core.md)
service to expose and/or publish a small normalized vehicle-energy snapshot. A
thin StarPilot adapter consumes `starpilotCarState`, avoiding a second reader on
the primary `carState` socket, while the transport remains independent of
controls and never writes CAN traffic.

Vehicle-specific CAN decoding belongs in opendbc and the vehicle port. This layer
only validates, caches, and transports normalized values.

## Normalized fields

The daemon currently consumes:

- `fuelGauge` as state of charge (`0.0...1.0`)
- `distanceToEmpty` in meters
- `charging`
- `chargingPortConnected`
- `vEgo` and `standstill` for upload cadence

At least one useful energy value is required. Default all-zero `CarState` values,
non-finite numbers, SOC outside `0...100%`, and DTE outside `0...900 km` are not
published as valid telemetry.

## Persistent cache

The latest valid snapshot is atomically written to:

```text
/data/galaxy/vehicle_telemetry_latest.json
```

The directory is owner-only (`0700`) and files are owner-only (`0600`). Unchanged
values are written only on the cache heartbeat to reduce flash writes. The Galaxy
endpoint labels data newer than 15 seconds `live`; older values remain available
as `cached`, including after the vehicle turns off or the daemon restarts.

## Configuration

Galaxy's **App Keys → EV Vehicle Telemetry** panel manages this configuration. The
same settings can be edited as an owner-only JSON file. StarPilot keeps its
existing location at `/data/galaxy/vehicle_telemetry_config.json`; the portable
core uses `/data/vehicle_telemetry` on stock openpilot.

For the quickest setup, connect the comma and phone to the same Wi-Fi network
while parked, then open **EV Vehicle Telemetry → Set Up** in Device settings on a
comma 3, or the **EV vehicle telemetry** QR action in comma 4 settings. Scan the QR
and choose **Personal public relay**. The temporary local page guides Tailscale
installation, owner login, Funnel approval, and fetch-token creation, then
shuts down after ten minutes or when the vehicle goes onroad. This restriction
applies only to configuration: the authenticated read-only telemetry API and
low-priority daemon remain available onroad. The setup page is not an
always-running Galaxy or Flask service. The full Galaxy panel remains available
for push and advanced FRP settings.

StarPilot supports five pull-transport modes:

- `off`: cache only; HTTPS push can still run.
- `local`: serve the authenticated API on the configured LAN port.
- `tailscale`: bind the API to loopback and publish it with a persistent public
  Funnel through the device owner's own free Tailscale account. This is the
  recommended public mode.
- `frp`: bind the API to loopback and supervise an FRP client that publishes a
  stable random subdomain through a self-hosted gateway.
- `galaxy`: let Galaxy serve the LAN and hosted-portal routes.

Create `/data/galaxy/vehicle_telemetry_config.json` as an owner-only file:

```json
{
  "schemaVersion": 1,
  "mode": "galaxy",
  "fetch": {
    "enabled": true,
    "token": "replace-with-at-least-32-random-characters"
  },
  "push": {
    "enabled": true,
    "url": "https://telemetry.example.com/v1/telemetry/ingest",
    "token": "replace-with-a-different-32-character-token",
    "vehicleId": "my-ev9",
    "vehicleName": "2026 Kia EV9",
    "maximumBatteryCapacityKilowattHours": 99.8,
    "drivingIntervalSeconds": 60,
    "chargingIntervalSeconds": 120,
    "parkedIntervalSeconds": 900
  }
}
```

The Galaxy panel can install and enable the personal relay, guide the owner
through Tailscale login and Funnel approval, rotate the fetch token, preserve
existing push/FRP secrets when their inputs are left blank, display tunnel
status, and copy the generated public URL. Tailscale binaries and state remain
under `/data`; setup never remounts the system partition or installs a systemd
unit. Disabling the relay removes only the telemetry-managed Funnel and retains
the owner's identity for easy re-enable. Gateway setup, wildcard DNS automation,
and FRP TLS guidance are documented in [the fork-neutral core guide](vehicle-telemetry-core.md#frp-mode).

Fetch and push are independent. Remove either section or set its `enabled` field
to `false` when it is not needed. Both tokens must be at least 32 characters;
fetch/push are disabled when their required token is missing or short. Push URLs
must be HTTPS, cannot include user info or fragments, and redirects are not
followed.

Legacy `/data/galaxy/telemetry_push.json` push configuration remains supported for
migration. An older combined `/data/galaxy/vehicle_telemetry.json` is accepted only
when it contains a `fetch` or `push` object; telemetry-shaped Galaxy cache files at
that path are never interpreted as configuration. New installations should use the
explicit `_config.json` filename.

## Fetch API

In `galaxy` mode, requests use Galaxy's port and routes below. In `local`,
`tailscale`, and `frp` modes the standalone core serves the same `/api/vehicle/telemetry` and
`/api/vehicle/telemetry/status` paths on the configured local or proxy URL.

Requests require the configured fetch bearer token:

```http
GET /api/galaxy/telemetry
Authorization: Bearer FETCH_TOKEN
```

`GET /api/vehicle/telemetry` is a compatibility alias. The diagnostic endpoint is:

```http
GET /api/vehicle/telemetry/status
Authorization: Bearer FETCH_TOKEN
```

Responses use `Cache-Control: no-store`. Disabled endpoints return `404`, invalid
authentication returns `401`, and an enabled endpoint without a validated cached
snapshot returns `503`. Diagnostic output reports token presence but never token
values.

### External app pairing

The Galaxy advertises `StarPilot Galaxy._sp-galaxy._tcp.local` on port 8082.
In **Galaxy → App Keys**, choose **Create Pairing QR**. The page shows both a QR
code and a six-digit code for 10 minutes. An external app can scan the QR, or use
mDNS to find the comma and submit the six-digit code.

Pairing creation and exchange accept only RFC1918/link-local/loopback clients and
local hostnames. A code is one-time, expires after 10 minutes, and is removed
after five invalid attempts. The QR contains only the local exchange URL and the
one-time code; it never contains a reusable bearer or Galaxy session.

```http
POST /api/external-app/pair
Content-Type: application/json

{
  "code": "123456",
  "clientName": "RangeBridge",
  "requestedCapabilities": ["vehicleTelemetry"]
}
```

The response supplies URLs for the active operating mode, the telemetry path,
and a client-specific bearer token. Local mode returns the standalone LAN port,
Tailscale and FRP modes return the ready HTTPS proxy URL, and Galaxy mode returns port 8082.
Each paired app gets its own token, so pairing a second app does not break the
first. RangeBridge requests `vehicleTelemetry` and `galaxySession` for LAN and
remote fallback. Galaxy Nav can use the same capability contract; only an
explicit `galaxySession` request returns the portal URL, cookie name, and Galaxy
session token after the one-time LAN exchange.

The hosted Galaxy tunnel preserves the device routing slug when forwarding API
requests. Remote telemetry therefore uses
`https://galaxy.firestar.link/<slug>/api/vehicle/telemetry` with both the paired
Galaxy cookie and the app-specific telemetry bearer. Galaxy exposes that exact
slug-prefixed route and rejects a slug that does not match the device's current
registration. LAN clients continue to use `/api/vehicle/telemetry` directly.

## Push API

StarPilot sends an authenticated JSON envelope to the configured URL. A live EV9
validation sample with vehicle name and battery capacity configured produced a
**465-byte JSON body** and a **651-byte prepared HTTP/1.1 request before TLS**:

```json
{
  "schemaVersion": 1,
  "vehicleId": "my-ev9",
  "sentAt": 1784235068410,
  "telemetry": {
    "schemaVersion": 1,
    "source": "StarPilot carState",
    "updatedAt": 1784235068.41,
    "stateOfChargePercent": 77.5,
    "distanceToEmptyKilometers": 408.0,
    "isCharging": false,
    "isPluggedIn": false
  }
}
```

At a 60-second driving interval, this is approximately **27.2 KiB/hour of JSON**
or **38.1 KiB/hour before TLS**. Including TLS/TCP overhead and connection setup,
budget roughly **0.1 to 0.2 MB per driving hour**. A 30-second interval doubles the
JSON body to about 54.5 KiB/hour. Parked at the default 15-minute interval, the
JSON body is under 2 KiB/hour. Connection behavior and mobile-network
retransmissions can raise actual on-wire usage.

The publisher uses openpilot's on-road state for driving cadence, so traffic lights
and other momentary stops do not create parked/driving transition uploads. Charging
takes priority over on-road state. It sends immediately on startup/activity
transitions, periodically at the configured activity interval, and when parked data
materially changes. Failed requests use bounded exponential backoff. Tokens remain
in headers and are never written into the payload or diagnostic status file.

## DBC information required for vehicle support

EV Vehicle Telemetry expects the vehicle DBC and `CarState` parser to provide:

- battery SOC mapped to `fuelGauge` as a `0.0...1.0` fraction;
- displayed distance to empty mapped to `distanceToEmpty` in meters;
- separate `charging` and `chargingPortConnected` booleans when available;
- the normal `vEgo` in meters per second and `standstill` boolean for cadence.

Stock openpilot v0.11.1 already contains `fuelGauge`, `charging`, `vEgo`, and
`standstill`. `distanceToEmpty` and `chargingPortConnected` are optional schema
extensions in StarPilot; the portable core safely omits them when running on an
unmodified stock schema.

For every new energy signal, document and test its CAN message/bus, start bit,
length, byte order, signedness, factor, offset, unit, expected frequency,
counter/checksum rules, and any validity bit. Reject stale/invalid frames, keep
plugged-in distinct from actively charging, and validate redundant cluster/BMS
sources when possible. A minimal port needs useful SOC or DTE; full RangeBridge
behavior benefits from all four energy/charging fields. The portable guide has
the complete [DBC and unit contract](vehicle-telemetry-core.md#dbc-and-vehicle-port-requirements).

## Adding vehicle support

1. Define the make/model CAN signals in opendbc.
2. Populate the normalized `CarState` fields in the vehicle interface.
3. When possible, validate redundant SOC/DTE sources before publishing them.
4. Add a focused DBC/CarState test for the supported fingerprint.
5. Do not add make/model decoding branches to the telemetry daemon.

This separation keeps the transport generic and makes new signals suitable for an
upstream opendbc/openpilot contribution.
