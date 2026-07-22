#!/usr/bin/env python3
"""Always-running bridge from generic CarState fields to EV telemetry transports."""

from __future__ import annotations

import time

from cereal import messaging

from openpilot.common.realtime import Ratekeeper
from openpilot.common.time_helpers import system_time_valid
from openpilot.system.vehicle_telemetry.core import (
  VehicleTelemetryCache,
  VehicleTelemetryConfigLoader,
  VehicleTelemetryPublisher,
  build_vehicle_telemetry_snapshot,
)


MAXIMUM_CACHED_PUBLISH_AGE_SECONDS = 30 * 24 * 60 * 60
DEFAULT_CAR_STATE_SERVICE = "carState"


def build_clock_valid_vehicle_telemetry_snapshot(car_state, vehicle_fingerprint="", timestamp=None, source_name=None, vin=""):
  """Build telemetry only after the comma has a trustworthy wall clock."""
  if not system_time_valid():
    return None
  wall_time = time.time() if timestamp is None else float(timestamp)  # noqa: TID251
  return build_vehicle_telemetry_snapshot(car_state, wall_time, vehicle_fingerprint, source_name=source_name, vin=vin)


def cached_snapshot_timestamp_is_plausible(snapshot, now=None):
  if not isinstance(snapshot, dict):
    return False
  try:
    updated_at = float(snapshot.get("updatedAt", 0.0))
  except (TypeError, ValueError):
    return False
  wall_time = time.time() if now is None else float(now)  # noqa: TID251
  age = wall_time - updated_at
  return 0.0 <= age <= MAXIMUM_CACHED_PUBLISH_AGE_SECONDS


def vehicle_telemetry_thread(
  *,
  car_state_service=DEFAULT_CAR_STATE_SERVICE,
  telemetry_available_field=None,
  source_name=None,
  config_path=None,
  support_state_callback=None,
):
  services = [car_state_service, "carParams", "deviceState"]
  sm = messaging.SubMaster(services)
  cache = VehicleTelemetryCache()
  publisher = VehicleTelemetryPublisher(config_path=config_path)
  publisher.start()
  config_loader = VehicleTelemetryConfigLoader(config_path)

  fingerprint = ""
  vin = ""
  was_off = False
  last_telemetry_available = None
  ratekeeper = Ratekeeper(1.0, None)
  while True:
    sm.update(0)
    config = config_loader.get()
    mode = config["mode"]

    if sm.updated["deviceState"] and sm.valid["deviceState"]:
      publisher.set_onroad(sm["deviceState"].started)
    if sm.updated["carParams"] and sm.valid["carParams"]:
      fingerprint = str(sm["carParams"].carFingerprint)
      vin = str(sm["carParams"].carVin)

    car_state = sm[car_state_service]
    telemetry_available = telemetry_available_field is None or bool(getattr(car_state, telemetry_available_field, False))
    state_is_valid = sm.updated[car_state_service] and sm.alive[car_state_service] and sm.valid[car_state_service]
    if support_state_callback is not None and state_is_valid and telemetry_available != last_telemetry_available:
      support_state_callback(telemetry_available)
      last_telemetry_available = telemetry_available

    if mode == "off":
      if not was_off:
        cache.clear()
      was_off = True
      ratekeeper.keep_time()
      continue
    was_off = False
    clock_valid = system_time_valid()
    if clock_valid and state_is_valid and telemetry_available:
      snapshot = build_clock_valid_vehicle_telemetry_snapshot(
        car_state,
        fingerprint,
        source_name=source_name,
        vin=vin,
      )
      if snapshot is not None:
        if mode == "galaxy":
          cache.store(snapshot)
        elif mode == "send":
          publisher.submit(snapshot)
    ratekeeper.keep_time()


def main():
  vehicle_telemetry_thread()


if __name__ == "__main__":
  main()
