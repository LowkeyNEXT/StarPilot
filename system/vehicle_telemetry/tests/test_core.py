import json

from types import SimpleNamespace

import pytest

from openpilot.system.vehicle_telemetry import core, daemon


@pytest.fixture(autouse=True)
def reset_runtime_adapter_state():
  core.reset_vehicle_telemetry_runtime()
  yield
  core.reset_vehicle_telemetry_runtime()


def test_snapshot_uses_generic_fields_and_automatic_vin():
  snapshot = core.build_vehicle_telemetry_snapshot(
    SimpleNamespace(
      fuelGauge=0.775, distanceToEmpty=408000.0, charging=True, chargingPortConnected=True,
      chargingTimeRemaining=18_000.0, vEgo=12.3456, standstill=False,
    ),
    timestamp=1234.5, vehicle_fingerprint="KIA EV9", vin="5xyaefs52tg015616",
  )
  assert snapshot == {
    "schemaVersion": 1, "source": "openpilot carState", "updatedAt": 1234.5,
    "vin": "5XYAEFS52TG015616", "vehicleFingerprint": "KIA EV9",
    "stateOfChargePercent": 77.5, "distanceToEmptyKilometers": 408.0,
    "isCharging": True, "isPluggedIn": True, "minutesToFull": 300,
    "speedMetersPerSecond": 12.346, "standstill": False,
  }
  assert "vin" not in core.build_vehicle_telemetry_snapshot(
    SimpleNamespace(fuelGauge=0.5), timestamp=1.0, vin="not-a-vin",
  )


def test_default_zero_car_state_is_not_valid_telemetry():
  assert core.build_vehicle_telemetry_snapshot(SimpleNamespace(fuelGauge=0.0, distanceToEmpty=0.0)) is None


def test_clock_validation_and_cached_timestamp(monkeypatch):
  state = SimpleNamespace(fuelGauge=0.5)
  monkeypatch.setattr(daemon, "system_time_valid", lambda: False)
  assert daemon.build_clock_valid_vehicle_telemetry_snapshot(state, timestamp=1000.0) is None

  monkeypatch.setattr(daemon, "system_time_valid", lambda: True)
  snapshot = daemon.build_clock_valid_vehicle_telemetry_snapshot(
    state, timestamp=2000.0, vin="5XYAEFS52TG015616",
  )
  assert snapshot["updatedAt"] == 2000.0
  assert snapshot["vin"] == "5XYAEFS52TG015616"
  assert daemon.cached_snapshot_timestamp_is_plausible({"updatedAt": 999.0}, now=1000.0)
  assert not daemon.cached_snapshot_timestamp_is_plausible({"updatedAt": 1001.0}, now=1000.0)


def test_config_has_only_off_galaxy_and_custom_modes(tmp_path):
  path = tmp_path / "config.json"
  galaxy = core.save_vehicle_telemetry_config(
    {"mode": "galaxy", "fetch": {"enabled": True, "token": "f" * 32}}, path,
  )
  assert galaxy["mode"] == "galaxy"
  assert galaxy["fetch"]["enabled"]
  assert not galaxy["push"]["enabled"]
  assert path.stat().st_mode & 0o077 == 0

  custom = core.save_vehicle_telemetry_config({
    "mode": "send",
    "fetch": {"enabled": True, "token": "f" * 32},
    "push": {"enabled": True, "url": "https://telemetry.example/ingest", "token": "p" * 32},
  }, path)
  assert custom["mode"] == "send"
  assert not custom["fetch"]["enabled"]
  assert custom["push"]["enabled"]

  for removed_mode in ("local", "tailscale", "frp"):
    assert core.save_vehicle_telemetry_config({"mode": removed_mode}, path)["mode"] == "off"


def test_public_config_redacts_secrets_and_has_no_manual_identity_or_intervals(tmp_path):
  saved = core.save_vehicle_telemetry_config({
    "mode": "send",
    "push": {
      "enabled": True, "url": "https://telemetry.example/ingest", "token": "p" * 32,
      "vehicleId": "ignored", "vehicleName": "ignored", "drivingIntervalSeconds": 1,
      "chargingIntervalSeconds": 1, "parkedIntervalSeconds": 900,
    },
  }, tmp_path / "config.json")
  public = core.public_vehicle_telemetry_config(saved)
  assert public["push"]["hasToken"]
  assert "token" not in public["push"]
  assert "vehicleId" not in public["push"]
  assert "vehicleName" not in public["push"]
  assert not any(key.endswith("IntervalSeconds") for key in public["push"])


def test_fetch_requires_galaxy_mode_and_long_bearer():
  config = core._normalize_vehicle_telemetry_config({
    "mode": "galaxy", "fetch": {"enabled": True, "token": "s" * 32},
  })
  assert core.is_fetch_authorized(config, f"Bearer {'s' * 32}")
  assert not core.is_fetch_authorized(config, f"Bearer {'x' * 32}")
  assert not core.is_fetch_authorized(
    {"mode": "send", "fetch": {"enabled": True, "token": "s" * 32}}, f"Bearer {'s' * 32}",
  )


def test_config_loader_bounds_idle_disk_reads():
  clock = [100.0]
  loads = []
  loader = core.VehicleTelemetryConfigLoader(
    reload_seconds=5.0, monotonic=lambda: clock[0],
    loader=lambda: loads.append(clock[0]) or core.default_vehicle_telemetry_config(),
  )
  assert loader.get() is loader.get()
  assert loads == [100.0]
  clock[0] += 5.0
  loader.get()
  assert loads == [100.0, 105.0]


def test_cache_heartbeat_legacy_alias_and_clear(tmp_path):
  cache_path = tmp_path / "latest.json"
  cache = core.VehicleTelemetryCache(cache_path, heartbeat_seconds=60)
  snapshot = {"schemaVersion": 1, "updatedAt": 1000.0, "stateOfChargePercent": 80.0}
  assert cache.store(snapshot, monotonic_now=100.0)
  assert not cache.store(snapshot, monotonic_now=120.0)
  assert cache.store(snapshot, monotonic_now=161.0)

  cache_path.write_text(json.dumps({
    "schemaVersion": 1, "source": "StarPilot carState", "updatedAt": 1000.0,
    "estimatedRangeKilometers": 408.0, "distanceToEmptyKilometers": 408.0,
  }))
  cache_path.chmod(0o600)
  assert "estimatedRangeKilometers" not in core.VehicleTelemetryCache(cache_path).load()
  cache.clear()
  assert cache.latest is None
  assert not cache_path.exists()


def test_saving_off_removes_cached_telemetry_and_status(tmp_path):
  config_path = tmp_path / core.VEHICLE_TELEMETRY_CONFIG_FILENAME
  cache_path = tmp_path / core.VEHICLE_TELEMETRY_CACHE_FILENAME
  status_path = tmp_path / core.VEHICLE_TELEMETRY_STATUS_FILENAME
  cache_path.write_text("{}")
  status_path.write_text("{}")
  core.save_vehicle_telemetry_config({"mode": "off"}, config_path)
  assert not cache_path.exists()
  assert not status_path.exists()


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


def test_publisher_uses_snapshot_vin_and_keeps_token_in_header(tmp_path):
  session = FakeSession()
  publisher = core.VehicleTelemetryPublisher(status_path=tmp_path / "status.json", session=session)
  push = core.default_vehicle_telemetry_config()["push"] | {
    "enabled": True, "url": "https://telemetry.example/ingest", "token": "t" * 32,
  }
  success, status = publisher._post(push, {
    "updatedAt": 1234.5, "vin": "5XYAEFS52TG015616",
    "vehicleFingerprint": "KIA EV9", "stateOfChargePercent": 80.0,
  })
  assert success and status == 202
  request = session.request[1]
  assert request["json"]["vehicleId"] == "5XYAEFS52TG015616"
  assert request["json"]["telemetry"]["vin"] == "5XYAEFS52TG015616"
  assert request["allow_redirects"] is False
  assert request["stream"] is True
  assert request["headers"]["Authorization"] == f"Bearer {'t' * 32}"
  assert "token" not in request["json"]
  assert session.response.closed


def test_fixed_publish_intervals_disable_parked_uploads():
  assert core.VEHICLE_TELEMETRY_DRIVING_INTERVAL_SECONDS == 60.0
  assert core.VEHICLE_TELEMETRY_CHARGING_INTERVAL_SECONDS == 120.0
  assert core.VEHICLE_TELEMETRY_PARKED_INTERVAL_SECONDS == 0.0


def test_live_cache_write_skips_fsync(tmp_path, monkeypatch):
  fsync_calls = []
  monkeypatch.setattr(core.os, "fsync", lambda descriptor: fsync_calls.append(descriptor))
  core.VehicleTelemetryCache(tmp_path / "latest.json").store({
    "schemaVersion": 1, "updatedAt": 1000.0, "stateOfChargePercent": 80.0,
  })
  assert fsync_calls == []
  core.save_vehicle_telemetry_config({"mode": "galaxy"}, tmp_path / "config.json")
  assert len(fsync_calls) == 1
