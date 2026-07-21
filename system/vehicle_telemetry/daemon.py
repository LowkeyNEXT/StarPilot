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
from openpilot.system.vehicle_telemetry.http_server import VehicleTelemetryHTTPService
from openpilot.system.vehicle_telemetry.tailscale import TAILSCALE_STATUS_FILENAME, TailscaleFunnelController
from openpilot.system.vehicle_telemetry.tunnel import FRPC_STATUS_FILENAME, FRPTunnelController


MAXIMUM_CACHED_PUBLISH_AGE_SECONDS = 30 * 24 * 60 * 60
DEFAULT_CAR_STATE_SERVICE = "carState"


def build_clock_valid_vehicle_telemetry_snapshot(car_state, vehicle_fingerprint="", timestamp=None, source_name=None):
  """Build telemetry only after the comma has a trustworthy wall clock."""
  if not system_time_valid():
    return None
  wall_time = time.time() if timestamp is None else float(timestamp)  # noqa: TID251
  return build_vehicle_telemetry_snapshot(car_state, wall_time, vehicle_fingerprint, source_name=source_name)


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


class StandaloneTransportController:
  def __init__(self, *, config_path=None, data_dir=None):
    self.frp = FRPTunnelController(data_dir=data_dir)
    self.tailscale = TailscaleFunnelController(data_dir=data_dir)
    self.http = VehicleTelemetryHTTPService(config_path=config_path)

  def reconcile(self, config):
    mode = config["mode"]
    fetch = config["fetch"]
    standalone = mode in ("local", "tailscale", "frp") and fetch["enabled"]
    status_path = self.tailscale.data_dir / TAILSCALE_STATUS_FILENAME if mode == "tailscale" else self.frp.data_dir / FRPC_STATUS_FILENAME
    self.http.set_tunnel_status_path(status_path)
    if standalone:
      bind_address = "127.0.0.1" if mode in ("tailscale", "frp") else fetch["bindAddress"]
      try:
        self.http.start(bind_address, fetch["port"])
      except OSError as error:
        self.http.stop()
        self.frp.stop(state="http-failed", error=error)
        self.tailscale.reconcile(False, config["tailscale"], fetch)
        return
    else:
      self.http.stop()
    self.frp.reconcile(mode == "frp" and standalone, config["tunnel"], fetch)
    self.tailscale.reconcile(mode == "tailscale" and standalone, config["tailscale"], fetch)

  def stop(self):
    self.http.stop()
    self.frp.stop()
    self.tailscale.stop()


def vehicle_telemetry_thread(
  *,
  car_state_service=DEFAULT_CAR_STATE_SERVICE,
  telemetry_available_field=None,
  source_name=None,
  config_path=None,
  data_dir=None,
):
  services = [car_state_service, "carParams", "deviceState"]
  sm = messaging.SubMaster(services)
  cache = VehicleTelemetryCache()
  publisher = VehicleTelemetryPublisher(config_path=config_path)
  publisher.start()
  transports = StandaloneTransportController(config_path=config_path, data_dir=data_dir)
  config_loader = VehicleTelemetryConfigLoader(config_path)
  cached_snapshot_pending = cache.latest

  fingerprint = ""
  ratekeeper = Ratekeeper(1.0, None)
  try:
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

      car_state = sm[car_state_service]
      telemetry_available = telemetry_available_field is None or bool(getattr(car_state, telemetry_available_field, False))
      if clock_valid and sm.updated[car_state_service] and sm.alive[car_state_service] and sm.valid[car_state_service] and telemetry_available:
        snapshot = build_clock_valid_vehicle_telemetry_snapshot(
          car_state,
          fingerprint,
          source_name=source_name,
        )
        if snapshot is not None:
          cache.store(snapshot)
          publisher.submit(snapshot)

      transports.reconcile(config_loader.get())
      ratekeeper.keep_time()
  finally:
    transports.stop()


def main():
  vehicle_telemetry_thread()


if __name__ == "__main__":
  main()
