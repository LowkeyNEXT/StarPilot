#!/usr/bin/env python3
"""StarPilot adapter from its single-consumer vehicle state to telemetry."""

from openpilot.common.params import Params
from openpilot.starpilot.system.vehicle_telemetry import (
  configure_starpilot_vehicle_telemetry,
  starpilot_vehicle_telemetry_identity,
)
from openpilot.system.vehicle_telemetry.daemon import (  # noqa: F401
  MAXIMUM_CACHED_PUBLISH_AGE_SECONDS,
  build_clock_valid_vehicle_telemetry_snapshot,
  cached_snapshot_timestamp_is_plausible,
  vehicle_telemetry_thread as core_vehicle_telemetry_thread,
)


VEHICLE_TELEMETRY_SERVICES = ["starpilotCarState", "carParams", "deviceState"]


def _set_vehicle_telemetry_supported(params, telemetry_available=False):
  persistent_supported, _ = starpilot_vehicle_telemetry_identity(params)
  supported = persistent_supported or telemetry_available
  params.put_bool("VehicleTelemetrySupported", supported)
  return supported


def vehicle_telemetry_thread():
  configure_starpilot_vehicle_telemetry()
  params = Params()
  _set_vehicle_telemetry_supported(params)
  core_vehicle_telemetry_thread(
    car_state_service="starpilotCarState",
    telemetry_available_field="vehicleTelemetryAvailable",
    source_name="StarPilot carState",
    support_state_callback=lambda available: _set_vehicle_telemetry_supported(params, available),
  )


def main():
  vehicle_telemetry_thread()


if __name__ == "__main__":
  main()
