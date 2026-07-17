#!/usr/bin/env python3
"""Always-running bridge from generic CarState energy fields to cache/export."""

import time

from cereal import messaging

from openpilot.common.realtime import Ratekeeper
from openpilot.starpilot.system.vehicle_telemetry import (
  VehicleTelemetryCache,
  VehicleTelemetryPublisher,
  build_vehicle_telemetry_snapshot,
)


def vehicle_telemetry_thread():
  sm = messaging.SubMaster(["carState", "carParams", "deviceState"])
  cache = VehicleTelemetryCache()
  publisher = VehicleTelemetryPublisher()
  publisher.start()
  if cache.latest:
    publisher.submit(cache.latest)

  fingerprint = ""
  ratekeeper = Ratekeeper(1.0, None)
  while True:
    sm.update(0)
    if sm.updated["deviceState"] and sm.valid["deviceState"]:
      publisher.set_onroad(sm["deviceState"].started)

    if sm.updated["carParams"] and sm.valid["carParams"]:
      fingerprint = str(sm["carParams"].carFingerprint)

    if sm.updated["carState"] and sm.alive["carState"] and sm.valid["carState"]:
      snapshot = build_vehicle_telemetry_snapshot(
        sm["carState"], time.time(), fingerprint,  # noqa: TID251 - interoperable wall-clock timestamp
      )
      if snapshot is not None:
        cache.store(snapshot)
        publisher.submit(snapshot)
    ratekeeper.keep_time()


def main():
  vehicle_telemetry_thread()


if __name__ == "__main__":
  main()
