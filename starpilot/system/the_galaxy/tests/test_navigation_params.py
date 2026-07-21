import json

import pytest

from openpilot.common.params import ParamKeyType
from openpilot.starpilot.system import vehicle_telemetry

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


def test_galaxy_configures_shared_telemetry_modes_without_exposing_secrets(monkeypatch, tmp_path):
  monkeypatch.setenv("SP_GALAXY_DIR", str(tmp_path))
  client, _ = _params_client(monkeypatch, {}, "tici")

  saved = client.post("/api/vehicle/telemetry/config", headers={"X-Galaxy-LAN-Setup": "1"}, json={
    "mode": "local",
    "rotateFetchToken": True,
    "fetch": {"enabled": True, "port": 17766},
    "pushToken": "p" * 32,
    "push": {
      "enabled": True,
      "url": "https://telemetry.example/ingest",
      "vehicleId": "my-ev",
    },
  })

  assert saved.status_code == 200
  payload = saved.get_json()
  assert payload["config"]["mode"] == "local"
  assert payload["config"]["fetch"]["bindAddress"] == "0.0.0.0"
  assert payload["config"]["fetch"]["port"] == 17766
  assert payload["config"]["fetch"]["hasToken"]
  assert len(payload["generatedFetchToken"]) >= 32
  assert "token" not in payload["config"]["fetch"]
  assert "token" not in payload["config"]["push"]
  assert vehicle_telemetry.vehicle_telemetry_config_path().stat().st_mode & 0o077 == 0

  preserved = client.post("/api/vehicle/telemetry/config", headers={"X-Galaxy-LAN-Setup": "1"}, json={
    "mode": "frp",
    "fetch": {"enabled": True, "port": 17766},
    "tunnelToken": "t" * 32,
    "tunnel": {
      "binaryPath": "/data/galaxy/bin/frpc",
      "serverAddress": "example.com",
      "serverPort": 7000,
      "subdomainHost": "example.com",
      "subdomain": "auto",
    },
  }).get_json()
  assert preserved["config"]["mode"] == "frp"
  assert preserved["config"]["push"]["enabled"]
  assert preserved["config"]["tunnel"]["hasToken"]


def test_telemetry_pairing_urls_follow_active_mode():
  config = vehicle_telemetry.default_vehicle_telemetry_config()
  config["fetch"]["port"] = 17766

  config["mode"] = "local"
  urls, path = the_galaxy._vehicle_telemetry_connection(config, "http://192.168.0.75:8082")
  assert urls == ["http://192.168.0.75:17766"]
  assert path == "/api/vehicle/telemetry"

  config["mode"] = "frp"
  urls, _ = the_galaxy._vehicle_telemetry_connection(config, "http://192.168.0.75:8082", {
    "state": "running",
    "publicURL": "https://vt-example.example.com/api/vehicle/telemetry",
  })
  assert urls == ["https://vt-example.example.com"]

  config["mode"] = "tailscale"
  urls, _ = the_galaxy._vehicle_telemetry_connection(config, "http://192.168.0.75:8082", {
    "state": "running",
    "publicURL": "https://vt-personal.example.ts.net/api/vehicle/telemetry",
  })
  assert urls == ["https://vt-personal.example.ts.net"]

  config["mode"] = "galaxy"
  urls, _ = the_galaxy._vehicle_telemetry_connection(config, "http://192.168.0.75:8082")
  assert urls == ["http://192.168.0.75:8082"]


def test_tailscale_setup_and_owner_login_are_lan_only(monkeypatch, tmp_path):
  monkeypatch.setenv("SP_GALAXY_DIR", str(tmp_path))
  client, _ = _params_client(monkeypatch, {}, "tici")

  remote = client.post(
    "/api/tailscale/setup",
    base_url="https://galaxy.firestar.link",
    environ_base={"REMOTE_ADDR": "203.0.113.10"},
  )
  assert remote.status_code == 403

  cross_site = client.post(
    "/api/tailscale/setup",
    base_url="http://192.168.0.75:8082",
    environ_base={"REMOTE_ADDR": "192.168.0.50"},
  )
  assert cross_site.status_code == 403

  config = vehicle_telemetry.default_vehicle_telemetry_config()
  config["mode"] = "tailscale"
  config["fetch"].update({"enabled": True, "token": "f" * 32})
  monkeypatch.setattr(the_galaxy, "enable_personal_tailscale_relay", lambda **kwargs: (config, "new-fetch-token" * 3))
  enabled = client.post(
    "/api/tailscale/setup",
    headers={"X-Galaxy-LAN-Setup": "1"},
    base_url="http://192.168.0.75:8082",
    environ_base={"REMOTE_ADDR": "192.168.0.50"},
  )
  assert enabled.status_code == 200
  assert enabled.get_json()["config"]["mode"] == "tailscale"
  assert enabled.get_json()["generatedFetchToken"] == "new-fetch-token" * 3

  vehicle_telemetry.save_vehicle_telemetry_config(config)
  monkeypatch.setattr(the_galaxy, "begin_tailscale_login", lambda tailscale, hostname: "https://login.tailscale.com/a/owner")
  login = client.post(
    "/api/tailscale/login",
    headers={"X-Galaxy-LAN-Setup": "1"},
    base_url="http://192.168.0.75:8082",
    environ_base={"REMOTE_ADDR": "192.168.0.50"},
  )
  assert login.status_code == 200
  assert login.get_json()["ownerURL"] == "https://login.tailscale.com/a/owner"


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


def test_external_app_pairing_is_lan_only_and_returns_six_digit_code(monkeypatch, tmp_path):
  monkeypatch.setenv("SP_GALAXY_DIR", str(tmp_path))
  slug_path = tmp_path / "glxyslug"
  session_path = tmp_path / "glxysession"
  slug_path.write_text("testGalaxySlug01")
  session_path.write_text("s" * 64)
  client, _ = _params_client(monkeypatch, {}, "tici")

  remote = client.post(
    "/api/external-app/pairing",
    base_url="https://galaxy.firestar.link",
    environ_base={"REMOTE_ADDR": "203.0.113.10"},
  )
  assert remote.status_code == 403

  created = client.post(
    "/api/external-app/pairing",
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
  created = client.post(
    "/api/external-app/pairing",
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
