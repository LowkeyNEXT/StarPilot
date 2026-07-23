import json

from types import SimpleNamespace

import pytest

from cereal import car, custom
from opendbc.car.hyundai.values import CAR
from openpilot.starpilot.system import vehicle_telemetry, vehicle_telemetryd
from openpilot.system.vehicle_telemetry import daemon as core_vehicle_telemetryd


@pytest.fixture(autouse=True)
def configure_starpilot_telemetry_adapter():
  vehicle_telemetry.configure_starpilot_vehicle_telemetry()


def test_snapshot_uses_starpilot_source_and_automatic_vin():
  snapshot = vehicle_telemetry.build_vehicle_telemetry_snapshot(
    SimpleNamespace(
      fuelGauge=0.775, distanceToEmpty=408000.0, charging=True, chargingPortConnected=True,
      chargingTimeRemaining=18_000.0, vEgo=12.3456, standstill=False,
    ),
    timestamp=1234.5, vehicle_fingerprint="KIA EV9", vin="5xyaefs52tg015616",
  )
  assert snapshot["source"] == "StarPilot carState"
  assert snapshot["vin"] == "5XYAEFS52TG015616"
  assert snapshot["stateOfChargePercent"] == 77.5
  assert snapshot["distanceToEmptyKilometers"] == 408.0
  assert snapshot["minutesToFull"] == 300


def test_default_zero_car_state_is_not_valid_telemetry():
  assert vehicle_telemetry.build_vehicle_telemetry_snapshot(
    SimpleNamespace(fuelGauge=0.0, distanceToEmpty=0.0),
  ) is None


def test_daemon_uses_starpilot_vehicle_state_without_another_car_state_reader():
  assert "starpilotCarState" in vehicle_telemetryd.VEHICLE_TELEMETRY_SERVICES
  assert "carState" not in vehicle_telemetryd.VEHICLE_TELEMETRY_SERVICES


def test_telemetry_support_bootstraps_from_persistent_car_params():
  cp = car.CarParams.new_message()
  cp.carFingerprint = CAR.KIA_EV9
  cp.carVin = "5xyaefs52tg015616"
  supported, vin = vehicle_telemetry.starpilot_vehicle_telemetry_identity(
    SimpleNamespace(get=lambda key: cp.to_bytes() if key == "CarParamsPersistent" else None),
  )
  assert supported
  assert vin == "5XYAEFS52TG015616"


def test_offroad_car_state_does_not_clear_persistent_telemetry_support():
  cp = car.CarParams.new_message()
  cp.carFingerprint = CAR.KIA_EV9
  values = {}
  params = SimpleNamespace(
    get=lambda key: cp.to_bytes() if key == "CarParamsPersistent" else None,
    put_bool=lambda key, value: values.__setitem__(key, value),
  )
  assert vehicle_telemetryd._set_vehicle_telemetry_supported(params, telemetry_available=False)
  assert values["VehicleTelemetrySupported"]


def test_telemetry_support_rejects_non_ev_platform():
  cp = car.CarParams.new_message()
  cp.carFingerprint = CAR.HYUNDAI_SANTA_FE_2022
  supported, vin = vehicle_telemetry.starpilot_vehicle_telemetry_identity(
    SimpleNamespace(get=lambda key: cp.to_bytes() if key == "CarParamsPersistent" else None),
  )
  assert not supported
  assert vin == ""


def test_starpilot_vehicle_state_carries_normalized_telemetry_fields():
  state = custom.StarPilotCarState.new_message()
  state.vehicleTelemetryAvailable = True
  state.fuelGauge = 0.775
  state.distanceToEmpty = 408000.0
  state.charging = True
  state.chargingPortConnected = True
  state.chargingTimeRemaining = 18_000.0
  snapshot = vehicle_telemetry.build_vehicle_telemetry_snapshot(state, timestamp=1234.5)
  assert snapshot["stateOfChargePercent"] == 77.5
  assert snapshot["distanceToEmptyKilometers"] == 408.0
  assert snapshot["isCharging"]
  assert snapshot["isPluggedIn"]
  assert snapshot["minutesToFull"] == 300


def test_daemon_does_not_timestamp_until_system_time_is_valid(monkeypatch):
  state = SimpleNamespace(fuelGauge=0.775)
  monkeypatch.setattr(core_vehicle_telemetryd, "system_time_valid", lambda: False)
  assert vehicle_telemetryd.build_clock_valid_vehicle_telemetry_snapshot(
    state, timestamp=1_000.0, vin="5XYAEFS52TG015616",
  ) is None

  monkeypatch.setattr(core_vehicle_telemetryd, "system_time_valid", lambda: True)
  snapshot = vehicle_telemetryd.build_clock_valid_vehicle_telemetry_snapshot(
    state, timestamp=2_000.0, vin="5XYAEFS52TG015616",
  )
  assert snapshot["updatedAt"] == 2_000.0
  assert snapshot["vin"] == "5XYAEFS52TG015616"


def test_activity_uses_onroad_state_and_prioritizes_charging():
  stopped = {"speedMetersPerSecond": 0.0, "standstill": True, "isCharging": False}
  moving = {"speedMetersPerSecond": 20.0, "standstill": False, "isCharging": False}
  assert vehicle_telemetry.vehicle_telemetry_activity(stopped, is_onroad=True) == "driving"
  assert vehicle_telemetry.vehicle_telemetry_activity(moving, is_onroad=False) == "parked"
  assert vehicle_telemetry.vehicle_telemetry_activity({**stopped, "isCharging": True}, is_onroad=True) == "charging"


def test_cache_reports_live_then_cached_and_can_be_cleared(tmp_path):
  cache_path = tmp_path / "latest.json"
  cache = vehicle_telemetry.VehicleTelemetryCache(cache_path, heartbeat_seconds=60)
  snapshot = {"schemaVersion": 1, "updatedAt": 1000.0, "stateOfChargePercent": 80.0}
  assert cache.store(snapshot, monotonic_now=100.0)
  assert vehicle_telemetry.telemetry_response(snapshot, now=1010.0)["availability"] == "live"
  assert vehicle_telemetry.telemetry_response(snapshot, now=1100.0)["availability"] == "cached"
  cache.clear()
  assert not cache_path.exists()


def test_legacy_starpilot_cache_drops_duplicated_range_alias(tmp_path):
  cache_path = tmp_path / "latest.json"
  cache_path.write_text(json.dumps({
    "schemaVersion": 1, "source": "StarPilot carState", "updatedAt": 1000.0,
    "estimatedRangeKilometers": 408.0, "distanceToEmptyKilometers": 408.0,
  }))
  cache_path.chmod(0o600)
  snapshot = vehicle_telemetry.VehicleTelemetryCache(cache_path).load()
  assert snapshot["distanceToEmptyKilometers"] == 408.0
  assert "estimatedRangeKilometers" not in snapshot


def test_telemetry_response_uses_snapshot_vin():
  response = vehicle_telemetry.telemetry_response({
    "schemaVersion": 1, "updatedAt": 1000.0, "vin": "5XYAEFS52TG015616",
    "stateOfChargePercent": 80.0,
  }, now=1010.0)
  assert response["vehicleId"] == "5XYAEFS52TG015616"
  assert response["vin"] == "5XYAEFS52TG015616"


def test_config_is_owner_only_https_and_secret_redacted(tmp_path):
  path = tmp_path / "vehicle_telemetry.json"
  saved = vehicle_telemetry.save_vehicle_telemetry_config({
    "mode": "send",
    "push": {"enabled": True, "url": "https://telemetry.example/ingest", "token": "p" * 32},
  }, path)
  assert saved["push"]["enabled"]
  assert path.stat().st_mode & 0o077 == 0
  public = vehicle_telemetry.public_vehicle_telemetry_config(saved)
  assert public["push"]["hasToken"]
  assert "token" not in public["push"]
  assert "vehicleId" not in public["push"]
  assert "vehicleName" not in public["push"]


def test_existing_galaxy_cache_is_not_treated_as_config(tmp_path, monkeypatch):
  cache_path = tmp_path / vehicle_telemetry.VEHICLE_TELEMETRY_LEGACY_COMBINED_CONFIG_FILENAME
  cache_path.write_text(json.dumps({
    "stateOfChargePercent": 90.0, "distanceToEmptyKilometers": 483.0, "updatedAt": 1234.5,
  }))
  cache_path.chmod(0o600)
  monkeypatch.setenv("SP_GALAXY_DIR", str(tmp_path))
  config = vehicle_telemetry.load_vehicle_telemetry_config()
  assert config["mode"] == "off"
  assert not config["fetch"]["enabled"]
  assert not config["push"]["enabled"]


def test_fetch_accepts_independent_external_app_tokens(tmp_path):
  config = vehicle_telemetry.save_vehicle_telemetry_config({
    "mode": "galaxy",
    "fetch": {
      "enabled": True,
      "clients": [
        {"name": "RangeBridge", "token": "r" * 32, "createdAt": 1234.0},
        {"name": "Galaxy Nav", "token": "g" * 32, "createdAt": 1235.0},
      ],
    },
  }, tmp_path / "config.json")
  assert vehicle_telemetry.is_fetch_authorized(config, f"Bearer {'r' * 32}")
  assert vehicle_telemetry.is_fetch_authorized(config, f"Bearer {'g' * 32}")
  public = vehicle_telemetry.public_vehicle_telemetry_config(config)
  assert public["fetch"]["pairedClientCount"] == 2
  assert "clients" not in public["fetch"]
