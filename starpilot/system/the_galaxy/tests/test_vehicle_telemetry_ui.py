from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[4]


def _source(relative_path: str) -> str:
  return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def test_galaxy_telemetry_ui_uses_one_time_pairing_without_session_secrets():
  navigation = _source("starpilot/system/the_galaxy/assets/components/navigation/navigation_keys.js")

  assert 'externalPairing: "/api/external-app/pairing"' in navigation
  assert 'telemetryConfig: "/api/vehicle/telemetry/config"' in navigation
  assert "Create Pairing QR" in navigation
  assert "One-time connection package" in navigation
  assert 'credentials: "same-origin"' in navigation
  assert "galaxySessionToken" not in navigation
  assert "sessionToken" not in navigation
  assert "galaxyIOSPairingCode" not in navigation
  assert "X-Galaxy-LAN-Setup" not in navigation


def test_galaxy_telemetry_ui_exposes_custom_schema_and_abrp_without_echoing_secrets():
  navigation = _source("starpilot/system/the_galaxy/assets/components/navigation/navigation_keys.js")

  assert "Custom JSON API" in navigation
  assert "A Better Routeplanner (ABRP)" in navigation
  assert "Build a custom JSON schema from selected telemetry variables" in navigation
  assert "fieldMappings" in navigation
  assert "ABRP telemetry API key (leave blank to keep)" in navigation
  assert "ABRP user token (leave blank to keep)" in navigation
  assert "Battery power is omitted" in navigation
  assert "https://web.abetterrouteplanner.com/resources/api" in navigation
  assert "https://documenter.getpostman.com/view/7396339/SWTK5a8w" in navigation
  assert "push.abrpApiKey" not in navigation
  assert "push.abrpUserToken" not in navigation


def test_tailscale_controls_use_owner_setup_cookie_flow():
  tailscale = _source("starpilot/system/the_galaxy/assets/components/tailscale/tailscale.js")

  assert 'requestJSON("/api/tailscale/installed")' in tailscale
  assert 'requestJSON("/api/tailscale/setup", { method: "POST" })' in tailscale
  assert 'requestJSON("/api/tailscale/login", { method: "POST" })' in tailscale
  assert 'credentials: "same-origin"' in tailscale
  assert "X-Galaxy-LAN-Setup" not in tailscale


def test_all_device_settings_launch_temporary_telemetry_setup():
  c3 = _source("selfdrive/ui/layouts/settings/device.py")
  c4 = _source("selfdrive/ui/mici/layouts/settings/galaxy.py")
  c4_settings = _source("selfdrive/ui/mici/layouts/settings/settings.py")
  qt = _source("selfdrive/ui/qt/offroad/settings.cc")

  assert "launch_vehicle_telemetry_setup" in c3
  assert "enabled=ui_state.is_offroad" in c3
  assert "launch_vehicle_telemetry_setup" in c4
  assert "telemetry_setup_btn.set_enabled(lambda: ui_state.is_offroad())" in c4_settings
  assert '"openpilot.system.vehicle_telemetry.setup", "launch"' in qt
  assert 'galaxy_dir + "/telemetry_setup_session.json"' in qt


def test_all_network_settings_use_standard_secure_bluetooth_obd_pairing():
  c3 = _source("selfdrive/ui/layouts/settings/bluetooth.py")
  c3_settings = _source("selfdrive/ui/layouts/settings/settings.py")
  c4 = _source("selfdrive/ui/mici/layouts/settings/galaxy.py")
  c4_settings = _source("selfdrive/ui/mici/layouts/settings/settings.py")
  c4_network = _source("selfdrive/ui/mici/layouts/settings/network/network_layout.py")
  qt = _source("selfdrive/ui/qt/network/networking.cc")
  process_config = _source("system/manager/process_config.py")

  for source in (c3, c4, qt):
    assert "ObdBlePairingRequested" in source
    assert "ObdBlePasskey" in source
    assert "ObdBleName" in source
    assert "passkey" in source.lower()
    assert "token" not in source[source.index("ObdBlePairingRequested"):source.index("ObdBlePairingRequested") + 1500].lower()

  assert "BluetoothNetworkSettings" in c3_settings
  assert "ObdBleBigButton" in c4_network
  assert "ObdBleBigButton" not in c4_settings
  assert "DEFAULT_OBD_BLE_NAME" in c3
  assert "DEFAULT_OBD_BLE_NAME" in c4
  assert 'name = "CommaOBD"' in qt
  assert 'BigParamControl("Bluetooth", "ObdBleEnabled"' in c4_network
  assert 'icons_mici/settings/network/bluetooth.png' in c4
  assert "Park before changing Bluetooth settings" not in c3
  assert 'self._bluetooth_toggle_btn.set_enabled(lambda: ui_state.is_offroad())' not in c4_network
  assert 'self._bluetooth_settings_btn.set_enabled(lambda: ui_state.is_offroad())' not in c4_network
  assert 'PythonProcess("obd_gatewayd", "starpilot.system.obd_gatewayd", always_run' in process_config
  for source in (c3, c4):
    assert "set_override_interactive_timeout" in source
    assert "PAIRING CODE" in source
    assert "PAIRED" in source
    assert "PAIR AGAIN" in source
    assert "_draw_progress_border" in source
  assert "ObdBleEnabled" not in _source("selfdrive/ui/layouts/settings/device.py")
  assert "ObdBleEnabled" not in _source("selfdrive/ui/qt/offroad/settings.cc")
