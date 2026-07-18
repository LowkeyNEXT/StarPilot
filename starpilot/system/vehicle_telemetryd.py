#!/usr/bin/env python3
"""Always-running bridge from generic CarState energy fields to cache/export."""

import time

from cereal import messaging

from openpilot.common.realtime import Ratekeeper
from openpilot.common.time_helpers import system_time_valid
from openpilot.starpilot.system.vehicle_telemetry import (
  VehicleTelemetryCache,
  VehicleTelemetryPublisher,
  build_vehicle_telemetry_snapshot,
)

MAXIMUM_CACHED_PUBLISH_AGE_SECONDS = 30 * 24 * 60 * 60


def build_clock_valid_vehicle_telemetry_snapshot(car_state, vehicle_fingerprint="", timestamp=None):
  """Build telemetry only after the comma has a trustworthy wall clock."""
  if not system_time_valid():
    return None
  wall_time = time.time() if timestamp is None else float(timestamp)  # noqa: TID251 - interoperable wall-clock timestamp
  return build_vehicle_telemetry_snapshot(car_state, wall_time, vehicle_fingerprint)


def cached_snapshot_timestamp_is_plausible(snapshot, now=None):
  if not isinstance(snapshot, dict):
    return False
  try:
    updated_at = float(snapshot.get("updatedAt", 0.0))
  except (TypeError, ValueError):
    return False
  wall_time = time.time() if now is None else float(now)  # noqa: TID251 - compare interoperable wall-clock timestamps
  age = wall_time - updated_at
  return 0.0 <= age <= MAXIMUM_CACHED_PUBLISH_AGE_SECONDS


def vehicle_telemetry_thread():
  sm = messaging.SubMaster(["carState", "carParams", "deviceState"])
  cache = VehicleTelemetryCache()
  publisher = VehicleTelemetryPublisher()
  publisher.start()
  cached_snapshot_pending = cache.latest

  fingerprint = ""
  ratekeeper = Ratekeeper(1.0, None)
  while True:
    sm.update(0)
    clock_valid = system_time_valid()
    if clock_valid and cached_snapshot_pending is not None:
      if cached_snapshot_timestamp_is_plausible(cached_snapshot_pending):
        publisher.submit(cached_snapshot_pending)
      cached_snapshot_pending = None

    if sm.updated["deviceState"] and sm.valid["deviceState"]:
      publisher.set_onroad(sm["deviceState"].started)

    if sm.updated["carParams"] and sm.valid["carParams"]:
      fingerprint = str(sm["carParams"].carFingerprint)

    if clock_valid and sm.updated["carState"] and sm.alive["carState"] and sm.valid["carState"]:
      snapshot = build_clock_valid_vehicle_telemetry_snapshot(sm["carState"], fingerprint)
      if snapshot is not None:
        cache.store(snapshot)
        publisher.submit(snapshot)
    ratekeeper.keep_time()


def main():
  vehicle_telemetry_thread()


if __name__ == "__main__":
  main()
