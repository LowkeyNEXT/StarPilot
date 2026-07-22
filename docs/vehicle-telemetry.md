# StarPilot EV Vehicle Telemetry

StarPilot exposes supported Hyundai/Kia/Genesis EV telemetry without placing CAN
or transport work in the driving-control path.

## Data flow

1. opendbc decodes SOC, DTE, charging, plug state, and charge time into generic
   CarState fields.
2. StarPilot copies those fields once into starpilotCarState and sets
   vehicleTelemetryAvailable only for verified platforms.
3. vehicle_telemetryd normalizes the snapshot and obtains VIN automatically from
   carParams.
4. The selected mode leaves telemetry off, makes it available through Galaxy,
   or sends it to one custom HTTPS backend.

The Galaxy App Keys page shows EV Vehicle Telemetry only when the current
fingerprint belongs to a verified telemetry platform. Existing Cookie Name and
Session Token controls remain unchanged.

## Setup

While parked, choose EV Vehicle Telemetry / Set Up on the comma and scan the QR.
The short-lived owner capability authorizes the Galaxy configuration page on the
local network. The handoff page contains no third-party assets or JavaScript
runtime dependency.

The Galaxy panel offers exactly Off, Galaxy portal, and Custom HTTPS backend.
The custom backend is mutually exclusive with Galaxy. It accepts an HTTPS URL,
bearer token, and optional battery capacity. VIN, vehicle name, and upload
intervals are not user settings. Off removes cached telemetry. Custom sending is
disabled while parked.

The info button summarizes the fields and privacy behavior without keeping setup
instructions visible all the time.

## Galaxy access

In Galaxy mode, external apps can use the one-time pairing QR. Pairing creates a
client-specific bearer token without removing the existing Galaxy cookie and
session fields. The authenticated endpoint is GET /api/vehicle/telemetry with
an Authorization: Bearer header. The hosted slug route has the same requirement,
and responses set Cache-Control: no-store.

## Supported HKG signals

Verified HKG support sets live in opendbc/car/hyundai/values.py. Parsers supply
fuelGauge as normalized SOC, distanceToEmpty in meters, charging,
chargingPortConnected, and chargingTimeRemaining in seconds.

Support should be expanded only after a route-backed decode is added to opendbc
and the platform is included in the corresponding verified support set.
