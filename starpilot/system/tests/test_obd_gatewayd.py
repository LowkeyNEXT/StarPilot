import asyncio
import ast

from pathlib import Path

from openpilot.starpilot.system.obd_gatewayd import (
  ObdGatewayStatus,
  _configured_obd_ble_name,
  _discard_control_requests,
  _run_enabled_gateway,
  _take_bool_request,
)


REPO_ROOT = Path(__file__).resolve().parents[3]


class FakeParams:
  def __init__(self, values=None):
    self.values = dict(values or {})

  def get_bool(self, key):
    return bool(self.values.get(key, False))

  def get(self, key):
    return self.values.get(key)

  def put_bool(self, key, value):
    self.values[key] = bool(value)

  def put(self, key, value):
    self.values[key] = value


def test_bool_control_request_is_consumed_once():
  params = FakeParams({"request": True})
  assert _take_bool_request(params, "request")
  assert not _take_bool_request(params, "request")


def test_disabled_gateway_discards_stale_control_edges():
  params = FakeParams({
    "ObdBlePairingRequested": True,
    "ObdBlePairingCancelRequested": True,
    "ObdBleForgetDevicesRequested": True,
  })
  _discard_control_requests(params)
  assert not any(params.get_bool(key) for key in params.values)


def test_legacy_obd_name_migrates_without_touching_bond_state():
  params = FakeParams({"ObdBleName": "StarPilot OBD"})
  assert _configured_obd_ble_name(params) == "CommaOBD"
  assert params.values["ObdBleName"] == "CommaOBD"


def test_status_never_publishes_passkey_or_application_credentials():
  params = FakeParams({"ObdBleEnabled": True})
  status = ObdGatewayStatus(params)
  status.set_passkey("123456")
  status.set_state("pairing")
  payload = params.values["ObdBleStatus"]
  assert isinstance(payload, dict)
  assert payload["secureConnectionsRequired"] is True
  assert "passkey" not in payload
  assert "token" not in payload
  assert params.values["ObdBlePasskey"] == "123456"


def test_enabled_gateway_opens_standard_pairing_window_and_stops_cleanly():
  params = FakeParams({
    "ObdBleEnabled": True,
    "ObdBlePairingRequested": True,
    "IsOnroad": True,
  })
  status = ObdGatewayStatus(params)
  events = []

  class Window:
    active = True
    remaining_seconds = 120

  class Peripheral:
    def __init__(self, *_args, passkey_callback, status_callback, local_name):
      self.pairing_window = Window()
      self.passkey_callback = passkey_callback
      self.status_callback = status_callback
      events.append(("name", local_name))

    async def start(self):
      events.append("start")

    async def set_pairing_window(self, enabled):
      events.append(("pairing", enabled))
      params.put_bool("ObdBleEnabled", False)

    async def remove_bonded_devices(self):
      events.append("forget")

    async def stop(self):
      events.append("stop")

  asyncio.run(_run_enabled_gateway(params, status, Peripheral, lambda: None))
  assert events == [("name", "CommaOBD"), "start", ("pairing", True), "stop"]
  assert params.values["ObdBlePasskey"] == ""


def test_gateway_source_has_no_vehicle_transport_imports():
  source = (REPO_ROOT / "starpilot/system/obd_gatewayd.py").read_text()
  tree = ast.parse(source)
  imported = {
    alias.name
    for node in ast.walk(tree)
    if isinstance(node, (ast.Import, ast.ImportFrom))
    for alias in node.names
  }
  assert not any("panda" in name.lower() for name in imported)
  assert not any("can" == name.lower() or name.lower().endswith(".can") for name in imported)
  assert "can_send" not in source
  assert "messaging" not in source


def test_process_config_registers_gateway_only_for_big_devices():
  source = (REPO_ROOT / "system/manager/process_config.py").read_text()
  assert 'PythonProcess("obd_gatewayd", "starpilot.system.obd_gatewayd"' in source
  assert 'if device_type in ("tici", "tizi", "mici")' in source
