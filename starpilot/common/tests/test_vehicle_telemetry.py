import json

from types import SimpleNamespace

from openpilot.starpilot.system import vehicle_telemetry


def test_build_vehicle_telemetry_snapshot_uses_generic_car_state_fields():
  snapshot = vehicle_telemetry.build_vehicle_telemetry_snapshot(SimpleNamespace(
    fuelGauge=0.775,
    distanceToEmpty=408000.0,
    charging=True,
    chargingPortConnected=True,
    vEgo=12.3456,
    standstill=False,
  ), timestamp=1234.5, vehicle_fingerprint="KIA EV9")

  assert snapshot == {
    "schemaVersion": 1,
    "source": "StarPilot carState",
    "updatedAt": 1234.5,
    "vehicleFingerprint": "KIA EV9",
    "stateOfChargePercent": 77.5,
    "estimatedRangeKilometers": 408.0,
    "distanceToEmptyKilometers": 408.0,
    "isCharging": True,
    "isPluggedIn": True,
    "speedMetersPerSecond": 12.346,
    "standstill": False,
  }


def test_default_zero_car_state_is_not_valid_telemetry():
  assert vehicle_telemetry.build_vehicle_telemetry_snapshot(SimpleNamespace(
    fuelGauge=0.0,
    distanceToEmpty=0.0,
  )) is None


def test_persistent_cache_reports_live_then_cached(tmp_path):
  cache_path = tmp_path / "latest.json"
  cache = vehicle_telemetry.VehicleTelemetryCache(cache_path, heartbeat_seconds=60)
  snapshot = {"schemaVersion": 1, "updatedAt": 1000.0, "stateOfChargePercent": 80.0}

  assert cache.store(snapshot, monotonic_now=100.0)
  assert not cache.store(snapshot, monotonic_now=120.0)
  assert cache.store(snapshot, monotonic_now=161.0)
  assert cache_path.stat().st_mode & 0o077 == 0
  assert vehicle_telemetry.VehicleTelemetryCache(cache_path).load() == snapshot
  assert vehicle_telemetry.telemetry_response(snapshot, now=1010.0)["availability"] == "live"
  assert vehicle_telemetry.telemetry_response(snapshot, now=1100.0)["availability"] == "cached"


def test_config_is_owner_only_https_and_secret_redacted(tmp_path):
  config_path = tmp_path / "vehicle_telemetry.json"
  raw = {
    "fetch": {"enabled": True, "token": "f" * 32},
    "push": {
      "enabled": True,
      "url": "https://telemetry.example/ingest",
      "token": "p" * 32,
      "vehicleId": "test-vehicle",
    },
  }
  saved = vehicle_telemetry.save_vehicle_telemetry_config(raw, config_path)
  assert saved["fetch"]["enabled"]
  assert saved["push"]["enabled"]
  assert config_path.stat().st_mode & 0o077 == 0

  public = vehicle_telemetry.public_vehicle_telemetry_config(saved)
  assert public["fetch"]["hasToken"]
  assert public["push"]["hasToken"]
  assert "token" not in public["fetch"]
  assert "token" not in public["push"]

  config_path.chmod(0o640)
  assert not vehicle_telemetry.load_vehicle_telemetry_config(config_path)["push"]["enabled"]


def test_legacy_cloudflare_push_config_is_supported(tmp_path, monkeypatch):
  legacy_path = tmp_path / vehicle_telemetry.VEHICLE_TELEMETRY_LEGACY_CONFIG_FILENAME
  legacy_path.write_text(json.dumps({
    "url": "https://telemetry.example/ingest",
    "token": "x" * 32,
    "vehicleId": "legacy-vehicle",
  }))
  legacy_path.chmod(0o600)
  monkeypatch.setenv("SP_GALAXY_DIR", str(tmp_path))

  config = vehicle_telemetry.load_vehicle_telemetry_config()
  assert config["push"]["enabled"]
  assert config["push"]["vehicleId"] == "legacy-vehicle"


def test_existing_galaxy_telemetry_cache_is_not_treated_as_config(tmp_path, monkeypatch):
  cache_path = tmp_path / vehicle_telemetry.VEHICLE_TELEMETRY_LEGACY_COMBINED_CONFIG_FILENAME
  cache_path.write_text(json.dumps({
    "stateOfChargePercent": 90.0,
    "distanceToEmptyKilometers": 483.0,
    "updatedAt": 1234.5,
  }))
  cache_path.chmod(0o600)
  monkeypatch.setenv("SP_GALAXY_DIR", str(tmp_path))

  config = vehicle_telemetry.load_vehicle_telemetry_config()
  assert not config["fetch"]["enabled"]
  assert not config["push"]["enabled"]


def test_legacy_combined_config_is_migrated_only_when_config_shaped(tmp_path, monkeypatch):
  legacy_path = tmp_path / vehicle_telemetry.VEHICLE_TELEMETRY_LEGACY_COMBINED_CONFIG_FILENAME
  legacy_path.write_text(json.dumps({
    "fetch": {"enabled": True, "token": "f" * 32},
  }))
  legacy_path.chmod(0o600)
  monkeypatch.setenv("SP_GALAXY_DIR", str(tmp_path))

  assert vehicle_telemetry.load_vehicle_telemetry_config()["fetch"]["enabled"]


def test_fetch_requires_long_bearer_token_and_constant_time_comparison(tmp_path):
  open_config = {"fetch": {"enabled": True, "token": ""}}
  short_config = {"fetch": {"enabled": True, "token": "short"}}
  token_config = {"fetch": {"enabled": True, "token": "s" * 32}}
  assert not vehicle_telemetry.save_vehicle_telemetry_config(open_config, tmp_path / "open.json")["fetch"]["enabled"]
  assert not vehicle_telemetry.save_vehicle_telemetry_config(short_config, tmp_path / "short.json")["fetch"]["enabled"]
  assert not vehicle_telemetry.is_fetch_authorized(open_config, None)
  assert not vehicle_telemetry.is_fetch_authorized(short_config, "Bearer short")
  assert vehicle_telemetry.is_fetch_authorized(token_config, f"Bearer {'s' * 32}")
  assert not vehicle_telemetry.is_fetch_authorized(token_config, f"Bearer {'x' * 32}")
  assert not vehicle_telemetry.is_fetch_authorized({"fetch": {"enabled": False}}, None)


class FakeResponse:
  status_code = 202

  def __init__(self):
    self.closed = False

  def close(self):
    self.closed = True


class FakeSession:
  def __init__(self):
    self.trust_env = True
    self.request = None
    self.response = FakeResponse()

  def post(self, *args, **kwargs):
    self.request = (args, kwargs)
    return self.response


def test_publisher_disables_redirects_and_keeps_token_in_header_only(tmp_path):
  session = FakeSession()
  publisher = vehicle_telemetry.VehicleTelemetryPublisher(status_path=tmp_path / "status.json", session=session)
  push = vehicle_telemetry.load_vehicle_telemetry_config(tmp_path / "missing.json")["push"] | {
    "enabled": True,
    "url": "https://telemetry.example/ingest",
    "token": "t" * 32,
    "vehicleId": "vehicle",
  }
  success, status = publisher._post(push, {"updatedAt": 1234.5, "stateOfChargePercent": 80.0})

  assert success and status == 202
  assert session.trust_env is False
  assert session.request[1]["allow_redirects"] is False
  assert session.request[1]["headers"]["Authorization"] == f"Bearer {'t' * 32}"
  assert "token" not in session.request[1]["json"]
  assert session.response.closed
