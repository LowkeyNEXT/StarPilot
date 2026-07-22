import io
import json
from datetime import UTC, datetime

import requests

from openpilot.system.vehicle_telemetry.setup import (
  TELEMETRY_SETUP_COOKIE_NAME,
  TELEMETRY_SETUP_STATUS_FILENAME,
  TelemetrySetupService,
  active_telemetry_setup_token,
  choose_lan_ipv4,
  launch_vehicle_telemetry_setup,
  main,
  render_setup_page,
)


def test_setup_selects_only_private_wifi_or_ethernet_addresses():
  addresses = [
    ("rmnet_data0", "10.20.30.40"),
    ("wlan0", "8.8.8.8"),
    ("eth0", "192.168.10.20"),
    ("wlan0", "192.168.1.50"),
  ]
  assert choose_lan_ipv4(addresses) == "192.168.1.50"
  assert choose_lan_ipv4([("rmnet_data0", "10.20.30.40")]) == ""


def test_setup_page_is_self_contained_galaxy_handoff():
  page = render_setup_page(600)
  assert "EV Vehicle Telemetry" in page
  assert "Open Galaxy settings" in page
  assert "/manage_navigation_keys" in page
  assert "Tailscale" not in page
  assert "FRP" not in page
  assert "Vehicle name" not in page
  assert "Vehicle ID" not in page
  assert "<script src=" not in page
  assert "<link" not in page
  assert "<img" not in page
  assert page.count("<script nonce=") == 1
  assert page.count("</script>") == 1


def test_launcher_keeps_setup_token_out_of_process_arguments(tmp_path):
  class FakeStdin(io.BytesIO):
    def close(self):
      self.captured = self.getvalue()
      super().close()

  class FakeProcess:
    pid = 4321

    def __init__(self):
      self.stdin = FakeStdin()

  process = FakeProcess()
  calls = []

  def popen(args, **kwargs):
    calls.append((args, kwargs))
    return process

  session = launch_vehicle_telemetry_setup(
    tmp_path,
    addresses=[("wlan0", "192.168.1.50")],
    duration_seconds=600,
    popen=popen,
    python_executable="python-test",
    startup_timeout=0,
  )
  args, kwargs = calls[0]
  assert args[:4] == ["python-test", "-m", "openpilot.system.vehicle_telemetry.setup", "serve"]
  assert session["url"].startswith("http://192.168.1.50:7767/?setup=")
  assert session["token"] not in " ".join(args)
  assert process.stdin.captured.decode().strip() == session["token"]
  assert kwargs["start_new_session"] is True
  status = json.loads((tmp_path / TELEMETRY_SETUP_STATUS_FILENAME).read_text())
  assert status["pid"] == 4321
  assert (tmp_path / TELEMETRY_SETUP_STATUS_FILENAME).stat().st_mode & 0o077 == 0


def test_live_setup_token_requires_owner_only_unexpired_running_session(tmp_path):
  token = "s" * 43
  path = tmp_path / TELEMETRY_SETUP_STATUS_FILENAME
  path.write_text(json.dumps({
    "state": "running",
    "pid": 123,
    "url": f"http://192.168.1.50:7767/?setup={token}",
    "expiresAt": 1600.0,
  }))
  path.chmod(0o600)

  from openpilot.system.vehicle_telemetry import setup
  original = setup._pid_is_running
  setup._pid_is_running = lambda pid: pid == 123
  try:
    assert active_telemetry_setup_token(tmp_path, wall_time=1000.0) == token
    assert active_telemetry_setup_token(tmp_path, wall_time=1601.0) == ""
    path.chmod(0o644)
    assert active_telemetry_setup_token(tmp_path, wall_time=1000.0) == ""
  finally:
    setup._pid_is_running = original


def test_launch_cli_never_prints_setup_capability(monkeypatch, capsys):
  secret = "s" * 43
  monkeypatch.setattr(
    "openpilot.system.vehicle_telemetry.setup.launch_vehicle_telemetry_setup",
    lambda *args, **kwargs: {
      "url": f"http://192.168.1.50:7767/?setup={secret}",
      "expiresAt": datetime.now(tz=UTC).timestamp() + 600,
      "pid": 4321,
    },
  )
  assert main(["launch"]) == 0
  output = capsys.readouterr().out
  assert secret not in output
  assert "?setup=" not in output
  assert json.loads(output)["host"] == "192.168.1.50"


def test_setup_http_requires_token_and_sets_httponly_cookie():
  token = "s" * 43
  service = TelemetrySetupService(token, datetime.now(tz=UTC).timestamp() + 600)
  try:
    service.start("127.0.0.1", 0)
    base = f"http://127.0.0.1:{service.port}"
    assert requests.get(base + "/", timeout=2.0).status_code == 404

    page = requests.get(base + f"/?setup={token}", timeout=2.0)
    assert page.status_code == 200
    assert page.headers["Cache-Control"] == "no-store"
    assert "default-src 'none'" in page.headers["Content-Security-Policy"]
    assert "HttpOnly" in page.headers["Set-Cookie"]
    assert "SameSite=Strict" in page.headers["Set-Cookie"]
    assert token not in page.text

    cookie = page.cookies.get(TELEMETRY_SETUP_COOKIE_NAME)
    assert requests.get(base + "/", cookies={TELEMETRY_SETUP_COOKIE_NAME: cookie}, timeout=2.0).status_code == 200
  finally:
    service.stop()
