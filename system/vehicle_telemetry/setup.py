#!/usr/bin/env python3
"""Temporary LAN authorization handoff for Galaxy telemetry settings."""

from __future__ import annotations

import argparse
import fcntl
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time

from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from openpilot.system.vehicle_telemetry.core import _atomic_write_json, load_vehicle_telemetry_status, vehicle_telemetry_dir


TELEMETRY_SETUP_PORT = 7767
TELEMETRY_SETUP_DURATION_SECONDS = 10 * 60
TELEMETRY_SETUP_MIN_DURATION_SECONDS = 2 * 60
TELEMETRY_SETUP_MAX_DURATION_SECONDS = 15 * 60
TELEMETRY_SETUP_STATUS_FILENAME = "telemetry_setup_session.json"
TELEMETRY_SETUP_LOCK_FILENAME = "telemetry_setup.lock"
TELEMETRY_SETUP_TOKEN_HEADER = "X-Telemetry-Setup"
TELEMETRY_SETUP_COOKIE_NAME = "openpilot_ev_telemetry_setup"

_PRIVATE_V4_NETWORKS = tuple(ipaddress.ip_network(network) for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"))
_INTERFACE_PRIORITY = ("wlan", "wifi", "eth", "en", "usb", "bridge", "ap")
_CELLULAR_INTERFACE_PREFIXES = ("rmnet", "wwan", "ccmni", "pdp", "cell")


def telemetry_setup_status_path(data_dir=None):
  return Path(data_dir or vehicle_telemetry_dir()) / TELEMETRY_SETUP_STATUS_FILENAME


def _owner_status(path):
  return load_vehicle_telemetry_status(path)


def _pid_is_running(pid):
  try:
    os.kill(int(pid), 0)
    return True
  except (OSError, TypeError, ValueError):
    return False


def active_telemetry_setup_token(data_dir=None, *, wall_time=None):
  status = _owner_status(telemetry_setup_status_path(data_dir))
  now = time.time() if wall_time is None else float(wall_time)  # noqa: TID251
  if status.get("state") not in ("starting", "running") or float(status.get("expiresAt") or 0.0) <= now:
    return ""
  if not _pid_is_running(status.get("pid")):
    return ""
  token = parse_qs(urlsplit(str(status.get("url") or "")).query).get("setup", [""])[0]
  return token if re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token) else ""


def telemetry_setup_token_is_authorized(supplied, data_dir=None, *, wall_time=None):
  active = active_telemetry_setup_token(data_dir, wall_time=wall_time)
  return bool(active) and hmac.compare_digest(str(supplied or ""), active)


def _interface_ipv4_addresses():
  addresses = []
  try:
    interfaces = socket.if_nameindex()
  except OSError:
    return addresses
  with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
    for _, name in interfaces:
      try:
        request = struct.pack("256s", name[:15].encode("ascii", errors="ignore"))
        response = fcntl.ioctl(probe.fileno(), 0x8915, request)
        addresses.append((name, socket.inet_ntoa(response[20:24])))
      except OSError:
        continue
  return addresses


def choose_lan_ipv4(addresses=None):
  candidates = []
  for interface, raw_address in addresses if addresses is not None else _interface_ipv4_addresses():
    interface = str(interface or "").lower()
    if interface.startswith(_CELLULAR_INTERFACE_PREFIXES):
      continue
    try:
      address = ipaddress.ip_address(str(raw_address or ""))
    except ValueError:
      continue
    if address.version != 4 or not any(address in network for network in _PRIVATE_V4_NETWORKS):
      continue
    priority = next((index for index, prefix in enumerate(_INTERFACE_PRIORITY) if interface.startswith(prefix)), None)
    if priority is not None:
      candidates.append((priority, interface, str(address)))
  return min(candidates)[2] if candidates else ""


def _device_is_onroad():
  try:
    from openpilot.common.params import Params
    return Params().get_bool("IsOnroad")
  except Exception:
    return False


def render_setup_page(duration_seconds, nonce="telemetry-setup"):
  del duration_seconds
  template = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><title>EV Vehicle Telemetry</title>
<style>
body { align-items:center; background:#0d0d18; color:#fff; display:flex; font:17px/1.45 system-ui,sans-serif;
  justify-content:center; margin:0; min-height:100vh; padding:24px; text-align:center; }
main { max-width:420px; } h1 { font-size:30px; } p { color:#b9b5c9; }
button { background:#8b6cc5; border:0; border-radius:12px; color:#fff; cursor:pointer; font:700 17px system-ui;
  margin-top:12px; min-height:52px; padding:12px 20px; width:100%; }
</style></head><body><main><h1>EV Vehicle Telemetry</h1>
<p>Continue in StarPilot Galaxy to configure telemetry.</p>
<button id="continue" type="button">Open Galaxy settings</button>
</main><script nonce="__NONCE__">
history.replaceState(null, "", location.pathname);
document.getElementById("continue").addEventListener("click", () => {
  const host = location.hostname.includes(":") ? `[${location.hostname}]` : location.hostname;
  location.href = `http://${host}:8082/manage_navigation_keys`;
});
</script></body></html>"""
  return template.replace("__NONCE__", nonce)


class TelemetrySetupRequestHandler(BaseHTTPRequestHandler):
  server_version = "openpilot-telemetry-setup/1"

  def log_message(self, *_args):
    pass

  def do_GET(self):
    parsed = urlsplit(self.path)
    cookie = SimpleCookie(self.headers.get("Cookie", ""))
    cookie_token = cookie.get(TELEMETRY_SETUP_COOKIE_NAME)
    supplied = parse_qs(parsed.query).get("setup", [""])[0] or (cookie_token.value if cookie_token is not None else "")
    if parsed.path != "/" or time.time() >= self.server.expires_at or not hmac.compare_digest(supplied, self.server.token):  # noqa: TID251
      self.send_error(404)
      return

    nonce = secrets.token_urlsafe(18)
    payload = render_setup_page(max(0, int(self.server.expires_at - time.time())), nonce).encode("utf-8")  # noqa: TID251
    self.send_response(200)
    self.send_header("Content-Type", "text/html; charset=utf-8")
    self.send_header("Content-Length", str(len(payload)))
    self.send_header("Cache-Control", "no-store")
    self.send_header("Content-Security-Policy", f"default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-{nonce}'; frame-ancestors 'none'")
    self.send_header("Referrer-Policy", "no-referrer")
    self.send_header("X-Content-Type-Options", "nosniff")
    if parse_qs(parsed.query).get("setup"):
      max_age = max(0, int(self.server.expires_at - time.time()))  # noqa: TID251
      self.send_header("Set-Cookie", f"{TELEMETRY_SETUP_COOKIE_NAME}={self.server.token}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Strict")
    self.end_headers()
    self.wfile.write(payload)


class _SetupHTTPServer(HTTPServer):
  allow_reuse_address = True

  def __init__(self, server_address, token, expires_at):
    self.token = token
    self.expires_at = float(expires_at)
    super().__init__(server_address, TelemetrySetupRequestHandler)


class TelemetrySetupService:
  def __init__(self, token, expires_at):
    self.token = str(token)
    self.expires_at = float(expires_at)
    self._server = None
    self._thread = None
    self.port = None

  def start(self, bind_address, port=TELEMETRY_SETUP_PORT):
    if self._server is not None:
      return
    self._server = _SetupHTTPServer((str(bind_address), int(port)), self.token, self.expires_at)
    self.port = self._server.server_address[1]
    self._thread = threading.Thread(target=self._server.serve_forever, name="telemetry-setup-http", daemon=True)
    self._thread.start()

  def stop(self):
    server, thread = self._server, self._thread
    self._server = None
    self._thread = None
    self.port = None
    if server is not None:
      server.shutdown()
      server.server_close()
    if thread is not None and thread is not threading.current_thread():
      thread.join(timeout=2.0)


def launch_vehicle_telemetry_setup(
  data_dir=None,
  *,
  addresses=None,
  port=TELEMETRY_SETUP_PORT,
  duration_seconds=TELEMETRY_SETUP_DURATION_SECONDS,
  popen=None,
  python_executable=None,
  startup_timeout=2.0,
):
  if _device_is_onroad():
    raise RuntimeError("EV Vehicle Telemetry setup is available only while parked.")
  address = choose_lan_ipv4(addresses)
  if not address:
    raise RuntimeError("Connect the comma to Wi-Fi before starting telemetry setup.")
  port = max(1024, min(65535, int(port)))
  duration_seconds = max(TELEMETRY_SETUP_MIN_DURATION_SECONDS, min(TELEMETRY_SETUP_MAX_DURATION_SECONDS, int(duration_seconds)))
  data_dir = Path(data_dir or vehicle_telemetry_dir())
  status_path = telemetry_setup_status_path(data_dir)
  now = time.time()  # noqa: TID251
  existing = _owner_status(status_path)
  parsed_existing = urlsplit(str(existing.get("url") or ""))
  if existing.get("expiresAt", 0) > now + 10 and _pid_is_running(existing.get("pid")) and parsed_existing.hostname == address:
    return existing | {"token": parse_qs(parsed_existing.query).get("setup", [""])[0]}

  token = secrets.token_urlsafe(32)
  expires_at = now + duration_seconds
  url = f"http://{address}:{port}/?setup={token}"
  command = [
    python_executable or sys.executable,
    "-m", "openpilot.system.vehicle_telemetry.setup", "serve",
    "--data-dir", str(data_dir), "--host", address, "--port", str(port), "--expires-at", str(expires_at),
  ]
  process = (popen or subprocess.Popen)(
    command,
    stdin=subprocess.PIPE,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    close_fds=True,
    start_new_session=True,
  )
  if process.stdin is None:
    raise RuntimeError("Could not securely start the telemetry setup session.")
  process.stdin.write((token + "\n").encode("ascii"))
  process.stdin.close()
  status = {"schemaVersion": 1, "state": "starting", "pid": process.pid, "url": url, "expiresAt": expires_at}
  current = _owner_status(status_path)
  if current.get("pid") != process.pid:
    _atomic_write_json(status_path, status)
  deadline = time.monotonic() + max(0.0, float(startup_timeout))
  while time.monotonic() < deadline:
    current = _owner_status(status_path)
    if current.get("pid") == process.pid and current.get("state") == "running":
      return current | {"token": token}
    if current.get("pid") == process.pid and current.get("state") == "error":
      raise RuntimeError(str(current.get("error") or "Telemetry setup could not start."))
    time.sleep(0.05)
  return status | {"token": token}


def serve_vehicle_telemetry_setup(data_dir, host, port, expires_at, token):
  if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", str(token or "")):
    raise RuntimeError("Invalid setup token.")
  if _device_is_onroad():
    raise RuntimeError("EV Vehicle Telemetry setup is available only while parked.")
  data_dir = Path(data_dir)
  data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
  lock_path = data_dir / TELEMETRY_SETUP_LOCK_FILENAME
  descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
  lock_file = os.fdopen(descriptor, "r+")
  try:
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
  except BlockingIOError as error:
    lock_file.close()
    raise RuntimeError("A telemetry setup session is already running.") from error

  try:
    try:
      os.setpriority(os.PRIO_PROCESS, 0, 19)
    except (AttributeError, OSError):
      pass
    service = TelemetrySetupService(token, expires_at)
    service.start(host, port)
    status_path = telemetry_setup_status_path(data_dir)
    _atomic_write_json(status_path, {
      "schemaVersion": 1, "state": "running", "pid": os.getpid(),
      "url": f"http://{host}:{service.port}/?setup={token}", "expiresAt": expires_at,
    })

    stop = threading.Event()
    def lifecycle():
      while not stop.wait(2.0):
        if time.time() >= expires_at or _device_is_onroad():  # noqa: TID251
          service.stop()
          return

    watcher = threading.Thread(target=lifecycle, name="telemetry-setup-lifecycle", daemon=True)
    watcher.start()
    try:
      if service._thread is not None:
        service._thread.join()
    finally:
      stop.set()
      service.stop()
      watcher.join(timeout=1.0)
      current = _owner_status(status_path)
      if current.get("pid") == os.getpid():
        status_path.unlink(missing_ok=True)
  finally:
    lock_file.close()


def main(argv=None):
  parser = argparse.ArgumentParser(description="Launch temporary Galaxy authorization for EV Vehicle Telemetry.")
  subparsers = parser.add_subparsers(dest="action", required=False)
  launch_parser = subparsers.add_parser("launch")
  launch_parser.add_argument("--data-dir")
  launch_parser.add_argument("--port", type=int, default=TELEMETRY_SETUP_PORT)
  launch_parser.add_argument("--duration", type=int, default=TELEMETRY_SETUP_DURATION_SECONDS)
  serve_parser = subparsers.add_parser("serve", help=argparse.SUPPRESS)
  serve_parser.add_argument("--data-dir", required=True)
  serve_parser.add_argument("--host", required=True)
  serve_parser.add_argument("--port", type=int, required=True)
  serve_parser.add_argument("--expires-at", type=float, required=True)
  arguments = parser.parse_args(argv)
  action = arguments.action or "launch"
  try:
    if action == "serve":
      token = sys.stdin.readline(256).strip()
      serve_vehicle_telemetry_setup(arguments.data_dir, arguments.host, arguments.port, arguments.expires_at, token)
      return 0
    session = launch_vehicle_telemetry_setup(arguments.data_dir, port=arguments.port, duration_seconds=arguments.duration)
    parsed_url = urlsplit(str(session.get("url") or ""))
    print(json.dumps({
      "expiresAt": session["expiresAt"], "host": parsed_url.hostname or "", "pid": session["pid"],
      "port": parsed_url.port, "state": "running",
    }, separators=(",", ":"), sort_keys=True))
    return 0
  except Exception as error:
    if action == "serve":
      status_path = telemetry_setup_status_path(arguments.data_dir)
      status = _owner_status(status_path)
      status.update({
        "schemaVersion": 1, "state": "error", "pid": os.getpid(),
        "expiresAt": arguments.expires_at, "error": str(error)[:240],
      })
      _atomic_write_json(status_path, status)
    print(json.dumps({"error": str(error)[:240]}, separators=(",", ":")))
    return 1


if __name__ == "__main__":
  raise SystemExit(main())
