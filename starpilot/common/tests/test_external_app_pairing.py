import base64
import json

import pytest

from openpilot.starpilot.system import external_app_pairing, vehicle_telemetry


@pytest.fixture(autouse=True)
def configure_starpilot_telemetry_adapter():
  vehicle_telemetry.configure_starpilot_vehicle_telemetry()


def test_one_time_pairing_issues_scoped_telemetry_connection(tmp_path, monkeypatch):
  monkeypatch.setenv("SP_GALAXY_DIR", str(tmp_path))
  created = external_app_pairing.create_pairing(tmp_path, "http://192.168.0.75:8082/settings", now=1000.0)
  assert created["qrData"].startswith(external_app_pairing.PAIRING_PREFIX)
  assert created["pairingCode"].isdigit() and len(created["pairingCode"]) == 6
  assert created["exchangeURL"] == "http://192.168.0.75:8082/api/external-app/pair"
  assert external_app_pairing.pairing_path(tmp_path).stat().st_mode & 0o077 == 0

  # Pull the code from the QR envelope exactly as an external app would.
  encoded = created["qrData"].removeprefix(external_app_pairing.PAIRING_PREFIX)
  encoded += "=" * (-len(encoded) % 4)
  qr_payload = json.loads(base64.urlsafe_b64decode(encoded))

  connection, error = external_app_pairing.complete_pairing(
    tmp_path,
    qr_payload["code"],
    "RangeBridge",
    requested_capabilities=["vehicleTelemetry", "galaxySession"],
    legacy_connection={
      "portalURL": "https://galaxy.example/device",
      "cookieName": "galaxy_session",
      "sessionToken": "opaque-session",
    },
    now=1001.0,
  )

  assert error is None
  telemetry = connection["capabilities"]["vehicleTelemetry"]
  assert telemetry["baseURLs"] == ["http://192.168.0.75:8082"]
  assert telemetry["path"] == "/api/vehicle/telemetry"
  assert len(telemetry["bearerToken"]) >= 32
  assert connection["capabilities"]["galaxySession"]["cookieName"] == "galaxy_session"
  config = vehicle_telemetry.load_vehicle_telemetry_config()
  assert config["mode"] == "galaxy"
  assert vehicle_telemetry.is_fetch_authorized(config, f"Bearer {telemetry['bearerToken']}")

  replay, replay_error = external_app_pairing.complete_pairing(
    tmp_path, qr_payload["code"], "Replay", now=1002.0,
  )
  assert replay is None
  assert "waiting" in replay_error


def test_expired_pairing_is_rejected_and_removed(tmp_path, monkeypatch):
  monkeypatch.setenv("SP_GALAXY_DIR", str(tmp_path))
  created = external_app_pairing.create_pairing(tmp_path, "http://comma.local:8082", now=1000.0)
  encoded = created["qrData"].removeprefix(external_app_pairing.PAIRING_PREFIX)
  encoded += "=" * (-len(encoded) % 4)
  code = json.loads(base64.urlsafe_b64decode(encoded))["code"]

  connection, error = external_app_pairing.complete_pairing(
    tmp_path, code, "RangeBridge", now=1000.0 + external_app_pairing.PAIRING_TTL_SECONDS + 1,
  )
  assert connection is None
  assert "expired" in error
  assert not external_app_pairing.pairing_path(tmp_path).exists()


def test_invalid_code_is_attempt_limited(tmp_path, monkeypatch):
  monkeypatch.setenv("SP_GALAXY_DIR", str(tmp_path))
  monkeypatch.setattr(external_app_pairing.secrets, "randbelow", lambda _: 123456)
  external_app_pairing.create_pairing(tmp_path, "http://comma.local:8082", now=1000.0)

  for attempt in range(external_app_pairing.MAX_PAIRING_ATTEMPTS):
    connection, error = external_app_pairing.complete_pairing(
      tmp_path, "999999", f"Attacker {attempt}", now=1001.0,
    )
    assert connection is None
    assert "invalid" in error

  assert not external_app_pairing.pairing_path(tmp_path).exists()
