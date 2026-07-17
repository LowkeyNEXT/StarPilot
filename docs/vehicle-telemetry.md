# Vehicle energy telemetry

StarPilot can expose and/or publish a small normalized vehicle-energy snapshot.
This service is independent of controls: it consumes generic `CarState` fields,
runs as the always-on low-priority `vehicle_telemetryd` process, and never writes
CAN traffic.

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

Create `/data/galaxy/vehicle_telemetry_config.json` as an owner-only file:

```json
{
  "schemaVersion": 1,
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

## Adding vehicle support

1. Define the make/model CAN signals in opendbc.
2. Populate the normalized `CarState` fields in the vehicle interface.
3. When possible, validate redundant SOC/DTE sources before publishing them.
4. Add a focused DBC/CarState test for the supported fingerprint.
5. Do not add make/model decoding branches to the telemetry daemon.

This separation keeps the transport generic and makes new signals suitable for an
upstream opendbc/openpilot contribution.
