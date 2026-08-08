#!/usr/bin/env python3
"""StarPilot lifecycle wrapper for the centralized secure BLE OBD gateway."""

from __future__ import annotations

import asyncio
import time

from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog
from openpilot.starpilot.system.vehicle_telemetry import VehicleTelemetryCache, configure_starpilot_vehicle_telemetry
from openpilot.system.vehicle_telemetry.obd import DEFAULT_OBD_BLE_NAME, ObdSessionManager, VirtualObdVehicle, normalize_obd_ble_name
from openpilot.system.vehicle_telemetry.obd_bluez import BluezObdPeripheral


CONTROL_POLL_SECONDS = 0.25
RETRY_SECONDS = 5.0
SNAPSHOT_RELOAD_SECONDS = 0.2
CONTROL_REQUEST_KEYS = (
  "ObdBlePairingRequested",
  "ObdBlePairingCancelRequested",
  "ObdBleForgetDevicesRequested",
)


class CachedSnapshotProvider:
  def __init__(self, cache=None, *, clock=time.monotonic):
    self.cache = cache or VehicleTelemetryCache()
    self.clock = clock
    self._last_load = float("-inf")
    self._latest = self.cache.latest

  def __call__(self):
    now = self.clock()
    if now - self._last_load >= SNAPSHOT_RELOAD_SECONDS:
      self._latest = self.cache.load()
      self._last_load = now
    return self._latest


class ObdGatewayStatus:
  def __init__(self, params):
    self.params = params
    self.state = "disabled"
    self.error = ""
    self.started_at = None
    self.local_name = DEFAULT_OBD_BLE_NAME

  def set_name(self, local_name):
    self.local_name = normalize_obd_ble_name(local_name)
    self.publish()

  def set_passkey(self, passkey):
    self.params.put("ObdBlePasskey", str(passkey or ""))

  def set_state(self, state):
    self.state = str(state)
    if self.state in ("ready", "pairing", "paired"):
      self.error = ""
    self.publish()

  def set_error(self, error):
    self.state = "error"
    self.error = str(error)[:240]
    self.publish()

  def publish(self, *, pairing_remaining=0):
    payload = {
      "schemaVersion": 1,
      "state": self.state,
      "enabled": self.params.get_bool("ObdBleEnabled"),
      "secureConnectionsRequired": True,
      "localName": self.local_name,
      "pairingRemainingSeconds": max(0, int(pairing_remaining)),
      "error": self.error,
      "updatedAtMonotonic": round(time.monotonic(), 3),
    }
    self.params.put("ObdBleStatus", payload)


def _take_bool_request(params, key):
  requested = params.get_bool(key)
  if requested:
    params.put_bool(key, False)
  return requested


def _discard_control_requests(params):
  for key in CONTROL_REQUEST_KEYS:
    _take_bool_request(params, key)


def _configured_obd_ble_name(params):
  raw_name = params.get("ObdBleName")
  local_name = normalize_obd_ble_name(raw_name)
  raw_text = raw_name.decode("utf-8", errors="ignore") if isinstance(raw_name, bytes) else str(raw_name or "")
  if raw_text != local_name:
    params.put("ObdBleName", local_name)
  return local_name


async def _run_enabled_gateway(params, status, peripheral_factory, snapshot_provider):
  local_name = _configured_obd_ble_name(params)
  status.set_name(local_name)
  vehicle = VirtualObdVehicle(snapshot_provider)
  sessions = ObdSessionManager(vehicle)
  peripheral = peripheral_factory(
    sessions,
    passkey_callback=status.set_passkey,
    status_callback=status.set_state,
    local_name=local_name,
  )
  pairing_open = False
  try:
    status.set_state("starting")
    await peripheral.start()
    while params.get_bool("ObdBleEnabled"):
      if _configured_obd_ble_name(params) != local_name:
        break
      if _take_bool_request(params, "ObdBlePairingRequested"):
        await peripheral.set_pairing_window(True)
        pairing_open = True

      if _take_bool_request(params, "ObdBlePairingCancelRequested") and pairing_open:
        await peripheral.set_pairing_window(False)
        pairing_open = False

      if _take_bool_request(params, "ObdBleForgetDevicesRequested"):
        if params.get_bool("IsOnroad"):
          status.set_error("Park before removing bonded Bluetooth devices")
        else:
          if pairing_open:
            await peripheral.set_pairing_window(False)
            pairing_open = False
          await peripheral.remove_bonded_devices()

      if pairing_open and not peripheral.pairing_window.active:
        await peripheral.set_pairing_window(False)
        pairing_open = False

      status.publish(pairing_remaining=peripheral.pairing_window.remaining_seconds)
      await asyncio.sleep(CONTROL_POLL_SECONDS)
  finally:
    await peripheral.stop()
    status.set_passkey("")


async def obd_gateway_loop(*, params=None, peripheral_factory=BluezObdPeripheral, sleep=asyncio.sleep):
  configure_starpilot_vehicle_telemetry()
  params = params or Params(return_defaults=True)
  status = ObdGatewayStatus(params)
  snapshot_provider = CachedSnapshotProvider()

  while True:
    if not params.get_bool("ObdBleEnabled"):
      # A disable can race the enabled loop and leave its cancel request set.
      # Do not let that stale edge immediately close the next pairing window.
      _discard_control_requests(params)
      status.set_passkey("")
      status.set_state("disabled")
      await sleep(CONTROL_POLL_SECONDS)
      continue
    try:
      await _run_enabled_gateway(params, status, peripheral_factory, snapshot_provider)
    except asyncio.CancelledError:
      raise
    except Exception as error:
      cloudlog.exception("secure BLE OBD gateway failed")
      status.set_error(error)
      await sleep(RETRY_SECONDS)


def main():
  asyncio.run(obd_gateway_loop())


if __name__ == "__main__":
  main()
