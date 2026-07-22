import json
import os
from datetime import UTC, datetime

import pytest

from openpilot.common.params import ParamKeyType
from openpilot.starpilot.system import vehicle_telemetry
from openpilot.system.vehicle_telemetry.setup import TELEMETRY_SETUP_COOKIE_NAME, TELEMETRY_SETUP_STATUS_FILENAME

from test_dashboard_stats import MODULE_DIR, _install_server_import_stubs


def _load_server_module():
  import importlib.util

  _install_server_import_stubs()
  spec = importlib.util.spec_from_file_location("navigation_params_server", MODULE_DIR / "the_galaxy.py")
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


the_galaxy = _load_server_module()


@pytest.fixture(autouse=True)
def configure_starpilot_telemetry_adapter():
  vehicle_telemetry.configure_starpilot_vehicle_telemetry()


class FakeParamsBackend:
  def __init__(self, key_types=None, default_values=None, values=None):
    self.key_types = key_types or {}
    self.default_values = default_values or {}
    self.values = values or {}
    self.writes = []

  def get_key_type(self, key):
    return self.key_types[key]

  def get_default_value(self, key):
    return self.default_values.get(key)

  def put(self, key, value):
    self.writes.append((key, value))
    self.values[key] = value

  def put_bool(self, key, value):
    self.writes.append((key, bool(value)))
    self.values[key] = bool(value)

  def get(self, key, block=False):
    return self.values.get(key)


class WritableFakeParams:
  def __init__(self, values=None):
    self.values = dict(values or {})
    self.writes = []
    self.removals = []

  def get(self, key, encoding=None, default=None, block=False):
    del encoding, block
    return self.values.get(key, default)

  def get_bool(self, key):
    value = self.values.get(key, False)
    if isinstance(value, bool):
      return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")

  def put(self, key, value):
    self.writes.append((key, value))
    self.values[key] = value

  def put_bool(self, key, value):
    self.writes.append((key, bool(value)))
    self.values[key] = bool(value)

  def get_int(self, key, default=0):
    return int(self.values.get(key, default))

  def put_int(self, key, value):
    self.writes.append((key, int(value)))
    self.values[key] = int(value)

  def remove(self, key):
    self.removals.append(key)
    self.values.pop(key, None)


def _params_client(monkeypatch, values, device_type):
  fake_params = WritableFakeParams(values)
  monkeypatch.setattr(the_galaxy, "params", fake_params)
  monkeypatch.setattr(
    the_galaxy,
    "_get_param_type_info",
    lambda: ({"UseOldUI", "TryRaylibUI"}, {"UseOldUI": bool, "TryRaylibUI": bool}),
  )
  monkeypatch.setattr(the_galaxy.HARDWARE, "get_device_type", lambda: device_type)
  monkeypatch.setattr(the_galaxy.Paths, "comma_home", lambda: "/tmp/dashboard-test-home", raising=False)

  assert the_galaxy._import_galaxy_web_symbols()
  app = the_galaxy.Flask(f"params_test_{device_type}")
  the_galaxy.setup(app)
  return app.test_client(), fake_params


def _active_setup_session(data_dir):
  token = "s" * 43
  path = data_dir / TELEMETRY_SETUP_STATUS_FILENAME
  path.write_text(json.dumps({
    "schemaVersion": 1,
    "state": "running",
    "pid": os.getpid(),
    "url": f"http://192.168.0.75:7767/?setup={token}",
    "expiresAt": datetime.now(tz=UTC).timestamp() + 600,
  }))
  path.chmod(0o600)
  return token


def test_galaxy_configures_only_off_portal_or_custom_backend(monkeypatch, tmp_path):
  monkeypatch.setenv("SP_GALAXY_DIR", str(tmp_path))
  client, _ = _params_client(monkeypatch, {}, "tici")
  token = _active_setup_session(tmp_path)
  vehicle_telemetry.save_vehicle_telemetry_config({
    "mode": "galaxy",
    "fetch": {"enabled": True, "token": "f" * 32},
  })

  galaxy = client.post("/api/vehicle/telemetry/config", headers={"X-Telemetry-Setup": token}, json={
    "mode": "galaxy",
  })
  assert galaxy.status_code == 200
  galaxy_config = galaxy.get_json()["config"]
  assert galaxy_config["mode"] == "galaxy"
  assert galaxy_config["fetch"]["enabled"]
  assert galaxy_config["fetch"]["hasToken"]
  assert not galaxy_config["push"]["enabled"]

  custom = client.post("/api/vehicle/telemetry/config", headers={"X-Telemetry-Setup": token}, json={
    "mode": "send",
    "pushToken": "p" * 32,
    "push": {
      "url": "https://telemetry.example/ingest",
      "vehicleId": "ignored",
      "vehicleName": "ignored",
      "parkedIntervalSeconds": 900,
    },
  })
  assert custom.status_code == 200
  custom_config = custom.get_json()["config"]
  assert custom_config["mode"] == "send"
  assert not custom_config["fetch"]["enabled"]
  assert custom_config["push"]["enabled"]
  assert custom_config["push"]["hasToken"]
  assert "token" not in custom_config["push"]
  assert "vehicleId" not in custom_config["push"]
  assert "vehicleName" not in custom_config["push"]
  assert "parkedIntervalSeconds" not in custom_config["push"]

  off = client.post("/api/vehicle/telemetry/config", headers={"X-Telemetry-Setup": token}, json={"mode": "off"})
  assert off.get_json()["config"]["mode"] == "off"
  assert not off.get_json()["config"]["fetch"]["enabled"]
  assert not off.get_json()["config"]["push"]["enabled"]


def test_removed_telemetry_modes_fail_closed(monkeypatch, tmp_path):
  monkeypatch.setenv("SP_GALAXY_DIR", str(tmp_path))
  client, _ = _params_client(monkeypatch, {}, "tici")
  token = _active_setup_session(tmp_path)
  for removed_mode in ("local", "tailscale", "frp"):
    response = client.post(
      "/api/vehicle/telemetry/config",
      headers={"X-Telemetry-Setup": token},
      json={"mode": removed_mode},
    )
    assert response.get_json()["config"]["mode"] == "off"


def test_telemetry_pairing_url_is_only_available_for_galaxy():
  config = vehicle_telemetry.default_vehicle_telemetry_config()
  urls, path = the_galaxy._vehicle_telemetry_connection(config, "http://192.168.0.75:8082")
  assert urls == []
  assert path == "/api/vehicle/telemetry"
  config["mode"] = "galaxy"
  urls, path = the_galaxy._vehicle_telemetry_connection(config, "http://192.168.0.75:8082")
  assert urls == ["http://192.168.0.75:8082"]
  assert path == "/api/vehicle/telemetry"


def test_galaxy_session_preserves_existing_cookie_fields_and_reports_support(monkeypatch, tmp_path):
  monkeypatch.setenv("SP_GALAXY_DIR", str(tmp_path))
  (tmp_path / "glxyauth").write_text("a" * 64)
  (tmp_path / "glxyslug").write_text("testGalaxySlug01")
  (tmp_path / "glxysession").write_text("s" * 64)
  monkeypatch.setattr(the_galaxy, "_vehicle_telemetry_identity", lambda: (True, "5XYAEFS52TG015616"))
  client, _ = _params_client(monkeypatch, {}, "tici")

  payload = client.get("/api/galaxy/session").get_json()

  assert payload["cookieName"] == "galaxy_session"
  assert payload["sessionToken"] == f"testGalaxySlug01%3A{'s' * 64}"
  assert payload["vehicleTelemetrySupported"] is True


def test_navigation_keys_keeps_existing_app_fields_and_simplifies_telemetry_ui():
  source = (MODULE_DIR / "assets/components/navigation/navigation_keys.js").read_text()
  assert "Cookie Name" in source
  assert "Session Token" in source
  assert '<option value="off">Off</option>' in source
  assert '<option value="galaxy">Galaxy portal</option>' in source
  assert '<option value="send">Custom HTTPS backend</option>' in source
  assert "vehicleTelemetrySupported" in source
  assert "telemetrySupported ?" in source
  assert "Send snapshots to a custom HTTPS backend" not in source
  assert "Vehicle name" not in source
  assert "Driving interval" not in source
  assert "Tailscale" not in source
  assert "FRP" not in source

def test_telemetry_configuration_mutation_is_lan_only(monkeypatch, tmp_path):
  monkeypatch.setenv("SP_GALAXY_DIR", str(tmp_path))
  client, _ = _params_client(monkeypatch, {}, "tici")
  remote = client.post(
    "/api/vehicle/telemetry/config",
    json={"mode": "local", "fetch": {"enabled": True}},
    base_url="https://galaxy.firestar.link",
    environ_base={"REMOTE_ADDR": "203.0.113.10"},
  )
  assert remote.status_code == 403

  unauthenticated_get = client.get(
    "/api/vehicle/telemetry/config",
    base_url="http://192.168.0.75:8082",
    environ_base={"REMOTE_ADDR": "192.168.0.50"},
  )
  assert unauthenticated_get.status_code == 403


def test_telemetry_configuration_accepts_live_httponly_setup_cookie(monkeypatch, tmp_path):
  monkeypatch.setenv("SP_GALAXY_DIR", str(tmp_path))
  client, _ = _params_client(monkeypatch, {}, "tici")
  token = _active_setup_session(tmp_path)
  client.set_cookie(TELEMETRY_SETUP_COOKIE_NAME, token, domain="192.168.0.75")

  response = client.get(
    "/api/vehicle/telemetry/config",
    base_url="http://192.168.0.75:8082",
    environ_base={"REMOTE_ADDR": "192.168.0.50"},
  )
  assert response.status_code == 200


def test_external_app_pairing_is_lan_only_and_returns_six_digit_code(monkeypatch, tmp_path):
  monkeypatch.setenv("SP_GALAXY_DIR", str(tmp_path))
  slug_path = tmp_path / "glxyslug"
  session_path = tmp_path / "glxysession"
  slug_path.write_text("testGalaxySlug01")
  session_path.write_text("s" * 64)
  client, _ = _params_client(monkeypatch, {}, "tici")
  token = _active_setup_session(tmp_path)
  vehicle_telemetry.save_vehicle_telemetry_config({
    "mode": "galaxy",
    "fetch": {"enabled": True, "token": "f" * 32},
  })

  remote = client.post(
    "/api/external-app/pairing",
    base_url="https://galaxy.firestar.link",
    environ_base={"REMOTE_ADDR": "203.0.113.10"},
  )
  assert remote.status_code == 403

  created = client.post(
    "/api/external-app/pairing",
    headers={"X-Telemetry-Setup": token},
    base_url="http://192.168.0.75:8082",
    environ_base={"REMOTE_ADDR": "192.168.0.50"},
  )
  assert created.status_code == 200
  pairing = created.get_json()
  assert pairing["pairingCode"].isdigit()
  assert len(pairing["pairingCode"]) == 6
  assert pairing["qrData"].startswith("starpilot-external-v1:")

  paired = client.post(
    "/api/external-app/pair",
    json={
      "code": pairing["pairingCode"],
      "clientName": "RangeBridge",
      "requestedCapabilities": ["vehicleTelemetry", "galaxySession"],
    },
    base_url="http://192.168.0.75:8082",
    environ_base={"REMOTE_ADDR": "192.168.0.51"},
  )
  assert paired.status_code == 200
  connection = paired.get_json()
  assert connection["capabilities"]["vehicleTelemetry"]["baseURLs"] == ["http://192.168.0.75:8082"]
  assert connection["capabilities"]["galaxySession"]["portalURL"] == "https://galaxy.firestar.link/testGalaxySlug01"
  assert connection["capabilities"]["galaxySession"]["cookieName"] == "galaxy_session"
  assert connection["capabilities"]["galaxySession"]["sessionToken"] == f"testGalaxySlug01%3A{'s' * 64}"
  bearer = connection["capabilities"]["vehicleTelemetry"]["bearerToken"]
  remote_telemetry = client.get(
    "/testGalaxySlug01/api/vehicle/telemetry",
    headers={"Authorization": f"Bearer {bearer}"},
  )
  assert remote_telemetry.status_code == 503
  wrong_route = client.get(
    "/wrongGalaxySlug/api/vehicle/telemetry",
    headers={"Authorization": f"Bearer {bearer}"},
  )
  assert wrong_route.status_code == 404


def test_external_app_pairing_requires_cloud_session_when_requested(monkeypatch, tmp_path):
  monkeypatch.setenv("SP_GALAXY_DIR", str(tmp_path))
  client, _ = _params_client(monkeypatch, {}, "tici")
  token = _active_setup_session(tmp_path)
  vehicle_telemetry.save_vehicle_telemetry_config({
    "mode": "galaxy",
    "fetch": {"enabled": True, "token": "f" * 32},
  })
  created = client.post(
    "/api/external-app/pairing",
    headers={"X-Telemetry-Setup": token},
    base_url="http://192.168.0.75:8082",
    environ_base={"REMOTE_ADDR": "192.168.0.50"},
  )

  paired = client.post(
    "/api/external-app/pair",
    json={
      "code": created.get_json()["pairingCode"],
      "clientName": "RangeBridge",
      "requestedCapabilities": ["vehicleTelemetry", "galaxySession"],
    },
    base_url="http://192.168.0.75:8082",
    environ_base={"REMOTE_ADDR": "192.168.0.51"},
  )

  assert paired.status_code == 409
  assert "cloud portal" in paired.get_json()["error"]

def test_params_compat_accepts_json_strings_for_json_keys():
  backend = FakeParamsBackend(
    key_types={"FavoriteDestinations": ParamKeyType.JSON},
    default_values={"FavoriteDestinations": []},
  )
  compat = the_galaxy.ParamsCompat(backend)

  compat.put("FavoriteDestinations", json.dumps([{"name": "Home"}]))

  assert backend.writes == [("FavoriteDestinations", [{"name": "Home"}])]


def test_params_compat_syncs_lead_indicator_inverse_key():
  backend = FakeParamsBackend()
  compat = the_galaxy.ParamsCompat(backend)

  compat.put_bool("LeadIndicator", True)

  assert backend.writes == [("LeadIndicator", True), ("HideLeadMarker", False)]


def test_params_compat_syncs_hide_lead_marker_inverse_key():
  backend = FakeParamsBackend()
  compat = the_galaxy.ParamsCompat(backend)

  compat.put_bool("HideLeadMarker", True)

  assert backend.writes == [("HideLeadMarker", True), ("LeadIndicator", False)]


def test_navigation_last_position_uses_recent_persisted_fix(monkeypatch):
  recent_payload = json.dumps({
    "latitude": 41.0,
    "longitude": -87.0,
    "hasFix": True,
    "updatedAtSec": 10_000.0,
  })
  memory_backend = FakeParamsBackend(values={"LastGPSPosition": ""})
  persisted_backend = FakeParamsBackend(values={"LastGPSPosition": recent_payload})

  monkeypatch.setattr(the_galaxy, "params_memory", the_galaxy.ParamsCompat(memory_backend))
  monkeypatch.setattr(the_galaxy, "params", the_galaxy.ParamsCompat(persisted_backend))
  monkeypatch.setattr(the_galaxy.time, "time", lambda: 10_300.0)
  monkeypatch.setattr(the_galaxy, "system_time_valid", lambda: True)

  position = the_galaxy._get_navigation_last_position()

  assert position["latitude"] == 41.0
  assert position["longitude"] == -87.0


def test_navigation_last_position_rejects_stale_persisted_fix(monkeypatch):
  stale_payload = json.dumps({
    "latitude": 41.0,
    "longitude": -87.0,
    "hasFix": True,
    "updatedAtSec": 10_000.0,
  })
  memory_backend = FakeParamsBackend(values={"LastGPSPosition": ""})
  persisted_backend = FakeParamsBackend(values={"LastGPSPosition": stale_payload})

  monkeypatch.setattr(the_galaxy, "params_memory", the_galaxy.ParamsCompat(memory_backend))
  monkeypatch.setattr(the_galaxy, "params", the_galaxy.ParamsCompat(persisted_backend))
  monkeypatch.setattr(the_galaxy.time, "time", lambda: 10_000.0 + the_galaxy.NAVIGATION_PERSISTED_LOCATION_MAX_AGE_SECONDS + 1.0)
  monkeypatch.setattr(the_galaxy, "system_time_valid", lambda: True)

  assert the_galaxy._get_navigation_last_position() is None


def test_save_longitudinal_maneuver_status_writes_json_param_as_dict(monkeypatch):
  fake_params = WritableFakeParams()
  monkeypatch.setattr(the_galaxy, "params", fake_params)

  saved = the_galaxy._save_longitudinal_maneuver_status({
    "state": "armed",
    "history": ["", "Started"],
  })

  assert fake_params.writes == [("LongitudinalManeuverStatus", saved)]
  assert isinstance(fake_params.writes[0][1], dict)
  assert saved["history"] == ["Started"]


def test_save_lateral_maneuver_status_writes_json_param_as_dict(monkeypatch):
  fake_params = WritableFakeParams()
  monkeypatch.setattr(the_galaxy, "params", fake_params)

  saved = the_galaxy._save_lateral_maneuver_status({
    "state": "armed",
    "history": ["", "Started"],
  })

  assert fake_params.writes == [("LateralManeuverStatus", saved)]
  assert isinstance(fake_params.writes[0][1], dict)
  assert saved["history"] == ["Started"]


def test_galaxy_session_value_matches_cookie_format():
  assert the_galaxy._build_galaxy_session_value(
    "testGalaxySlug01",
    "a" * 64,
  ) == f"testGalaxySlug01%3A{'a' * 64}"


def test_configured_favorite_slot_values_only_reads_selected_keys(monkeypatch):
  fake_params = WritableFakeParams({
    "NavDesiresAllowed": False,
    "RedneckCruise": True,
    "UnusedToggle": True,
  })
  monkeypatch.setattr(the_galaxy, "params", fake_params)

  values = the_galaxy._configured_favorite_slot_values([
    {"enabled": True, "key": "NavDesiresAllowed"},
    {"enabled": False, "key": "RedneckCruise"},
    {"enabled": False, "key": None},
  ])

  assert values == {"NavDesiresAllowed": False, "RedneckCruise": True}


def test_favorite_values_endpoint_returns_current_selected_value(monkeypatch):
  client, _ = _params_client(monkeypatch, {"UseOldUI": False}, "tici")
  monkeypatch.setattr(the_galaxy, "_get_favorite_slot_options", lambda: [{"key": "UseOldUI"}])
  monkeypatch.setattr(
    the_galaxy,
    "normalize_favorite_slots",
    lambda *args, **kwargs: [{"enabled": True, "key": "UseOldUI"}],
  )

  response = client.get("/api/favorites/values")

  assert response.status_code == 200
  assert response.get_json() == {"values": {"UseOldUI": False}}


def test_favorite_slot_options_include_virtual_cruise_actions(monkeypatch):
  monkeypatch.setattr(the_galaxy, "_favorite_slot_options", None)
  monkeypatch.setattr(the_galaxy, "_get_param_type_info", lambda: (set(), {}))

  options = the_galaxy._get_favorite_slot_options()
  option_keys = {option["key"] for option in options}

  assert "__starpilot_favorite_action__:distance_decrease" in option_keys
  assert "__starpilot_favorite_action__:distance_increase" in option_keys


def test_favorite_action_endpoint_increments_virtual_button_counter(monkeypatch):
  client, _ = _params_client(monkeypatch, {}, "tici")
  fake_memory = WritableFakeParams()
  monkeypatch.setattr(the_galaxy, "params_memory", fake_memory)

  response = client.post("/api/favorites/action", json={"key": "__starpilot_favorite_action__:distance_increase"})

  assert response.status_code == 200
  assert fake_memory.get_int("FavoriteVirtualAccelCruiseCounter") == 1


def test_use_old_ui_is_noop_on_c4_mici(monkeypatch):
  client, fake_params = _params_client(monkeypatch, {"UseOldUI": False, "IsOnroad": False}, "mici")

  response = client.put("/api/params", json={"key": "UseOldUI", "value": True})
  payload = response.get_json()

  assert response.status_code == 200
  assert payload["updated"] == {"UseOldUI": False, "TryRaylibUI": False}
  assert fake_params.values["UseOldUI"] is False
  assert fake_params.writes == []


def test_use_old_ui_writes_on_big_device_offroad(monkeypatch):
  client, fake_params = _params_client(monkeypatch, {"UseOldUI": False, "TryRaylibUI": True, "IsOnroad": False}, "tici")

  response = client.put("/api/params", json={"key": "UseOldUI", "value": True})
  payload = response.get_json()

  assert response.status_code == 200
  assert payload["updated"] == {"UseOldUI": True, "TryRaylibUI": False}
  assert fake_params.values["UseOldUI"] is True
  assert fake_params.values["TryRaylibUI"] is False
  assert fake_params.writes == [("UseOldUI", True), ("TryRaylibUI", False)]


def test_use_old_ui_rejects_big_device_onroad_change(monkeypatch):
  client, fake_params = _params_client(monkeypatch, {"UseOldUI": False, "TryRaylibUI": True, "IsOnroad": True}, "tici")

  response = client.put("/api/params", json={"key": "UseOldUI", "value": True})

  assert response.status_code == 403
  assert response.get_json()["error"] == "Cannot change Use Old UI while driving."
  assert fake_params.values["UseOldUI"] is False
  assert fake_params.values["TryRaylibUI"] is True
  assert fake_params.writes == []


def test_legacy_try_raylib_ui_payload_updates_use_old_ui(monkeypatch):
  client, fake_params = _params_client(monkeypatch, {"UseOldUI": True, "TryRaylibUI": False, "IsOnroad": False}, "tici")

  response = client.put("/api/params", json={"key": "TryRaylibUI", "value": True})
  payload = response.get_json()

  assert response.status_code == 200
  assert payload["updated"] == {"UseOldUI": False, "TryRaylibUI": True}
  assert fake_params.values["UseOldUI"] is False
  assert fake_params.values["TryRaylibUI"] is True
  assert fake_params.writes == [("UseOldUI", False), ("TryRaylibUI", True)]


def test_curve_speed_controller_reset_clears_learned_data_offroad(monkeypatch):
  client, fake_params = _params_client(monkeypatch, {
    "IsOnroad": False,
    "CalibratedLateralAcceleration": 2.73,
    "CalibrationProgress": 48.0,
    "CurvatureData": {"0.01": {"average": 2.73, "count": 12}},
  }, "tici")

  response = client.post("/api/curve_speed_controller/reset")

  assert response.status_code == 200
  assert response.get_json()["updated"] == {
    "CalibratedLateralAcceleration": 2.0,
    "CalibrationProgress": 0.0,
  }
  assert fake_params.values["CalibratedLateralAcceleration"] == 2.0
  assert "CalibrationProgress" not in fake_params.values
  assert "CurvatureData" not in fake_params.values
  assert fake_params.removals == ["CalibrationProgress", "CurvatureData"]


def test_curve_speed_controller_reset_rejected_onroad(monkeypatch):
  client, fake_params = _params_client(monkeypatch, {
    "IsOnroad": True,
    "CalibratedLateralAcceleration": 2.73,
    "CalibrationProgress": 48.0,
    "CurvatureData": {"0.01": {"average": 2.73, "count": 12}},
  }, "tici")

  response = client.post("/api/curve_speed_controller/reset")

  assert response.status_code == 403
  assert response.get_json()["error"] == "Curve Speed Controller data can only be reset while parked."
  assert fake_params.writes == []
  assert fake_params.removals == []
