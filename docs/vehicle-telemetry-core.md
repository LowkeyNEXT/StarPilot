# EV Vehicle Telemetry core

The shared core converts generic CarState energy fields into a small read-only
snapshot. Vehicle-specific CAN decoding remains in opendbc. The daemon runs at
1 Hz and nice level 19, outside the controls process.

## Modes

| Mode | Behavior |
| --- | --- |
| off | No sampling, cache, API data, or upload. Saving off removes the prior snapshot and exporter status. |
| galaxy | Cache the latest supported snapshot for the fork-owned authenticated Galaxy API. |
| send | Send snapshots to one custom HTTPS endpoint. No inbound telemetry API is enabled. |

Legacy local, tailscale, and frp values fail closed to off.

Custom HTTPS sending uses fixed cadence: 60 seconds while driving, 120 seconds
while charging, and 0 while parked. These values are intentionally not
configurable. The bearer token is sent only in the Authorization header,
redirects are disabled, proxy environment variables are ignored, and the
response body is never buffered.

## Identity and payload

The daemon reads the VIN already populated in carParams.carVin. A valid
17-character VIN is normalized to uppercase and used as both vin in the
snapshot and vehicleId in the custom-backend envelope. There is no manual
vehicle ID or vehicle name setting. Before a valid VIN arrives, the custom
envelope falls back to carFingerprint.

Normalized fields are stateOfChargePercent, distanceToEmptyKilometers,
isCharging, isPluggedIn, minutesToFull, speedMetersPerSecond, standstill, vin,
and vehicleFingerprint.

A snapshot is rejected until at least a non-zero SOC or valid DTE exists, which
prevents unsupported vehicles' schema defaults from appearing as real data.

## Configuration

The neutral core stores owner-only configuration under
/data/vehicle_telemetry/vehicle_telemetry_config.json. Fork adapters may supply
another directory. Public config responses redact tokens.

The send mode accepts an HTTPS URL, a bearer token of at least 32 characters,
and optional maximumBatteryCapacityKilowattHours. All config, status, setup
capability, and telemetry files use owner-only permissions.

## Integration

A fork provides its storage directory, source name, and optional availability
field through configure_vehicle_telemetry_runtime and vehicle_telemetry_thread.
The fork-owned API supplies authentication and routing in galaxy mode.
