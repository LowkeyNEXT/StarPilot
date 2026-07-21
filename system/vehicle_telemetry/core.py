#!/usr/bin/env python3
"""Normalized vehicle-energy telemetry, persistent cache, and exporter.

Vehicle-specific CAN decoding belongs in opendbc. This module consumes generic
CarState-like fields and deliberately runs outside the driving control loop.
"""

from __future__ import annotations

import hmac
import json
import math
import os
import re
import threading
import time

from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

import requests


VEHICLE_TELEMETRY_SCHEMA_VERSION = 1
VEHICLE_TELEMETRY_CONFIG_FILENAME = "vehicle_telemetry_config.json"
VEHICLE_TELEMETRY_LEGACY_COMBINED_CONFIG_FILENAME = "vehicle_telemetry.json"
VEHICLE_TELEMETRY_LEGACY_CONFIG_FILENAME = "telemetry_push.json"
VEHICLE_TELEMETRY_CACHE_FILENAME = "vehicle_telemetry_latest.json"
VEHICLE_TELEMETRY_STATUS_FILENAME = "vehicle_telemetry_status.json"
VEHICLE_TELEMETRY_LIVE_SECONDS = 15.0
VEHICLE_TELEMETRY_HEARTBEAT_SECONDS = 60.0
VEHICLE_TELEMETRY_DEFAULT_PORT = 7766
VEHICLE_TELEMETRY_CONFIG_RELOAD_SECONDS = 5.0
TELEMETRY_MODES = ("off", "send", "local", "tailscale", "frp", "galaxy")

_TELEMETRY_FIELDS = (
  "source",
  "updatedAt",
  "vehicleFingerprint",
  "stateOfChargePercent",
  "estimatedRangeKilometers",
  "distanceToEmptyKilometers",
  "isCharging",
  "isPluggedIn",
  "speedMetersPerSecond",
  "standstill",
)

_data_dir_provider: Callable[[], Path] | None = None
_legacy_fetch_mode = "local"
_source_name = "openpilot carState"


def reset_vehicle_telemetry_runtime():
  global _data_dir_provider, _legacy_fetch_mode, _source_name
  _data_dir_provider = None
  _legacy_fetch_mode = "local"
  _source_name = "openpilot carState"


def configure_vehicle_telemetry_runtime(*, data_dir_provider=None, legacy_fetch_mode=None, source_name=None):
  """Configure process-local integration hooks without importing a fork.

  Fork adapters call this once during process startup. Keeping the hooks here
  lets the core package remain directly cherry-pickable onto stock openpilot.
  """
  global _data_dir_provider, _legacy_fetch_mode, _source_name
  if data_dir_provider is not None:
    _data_dir_provider = data_dir_provider
  if legacy_fetch_mode is not None:
    if legacy_fetch_mode not in TELEMETRY_MODES:
      raise ValueError(f"Unsupported legacy telemetry mode: {legacy_fetch_mode}")
    _legacy_fetch_mode = legacy_fetch_mode
  if source_name is not None:
    _source_name = str(source_name or "openpilot carState")[:80]


def _default_vehicle_telemetry_dir() -> Path:
  if override := os.getenv("OPENPILOT_VEHICLE_TELEMETRY_DIR"):
    return Path(override)
  from openpilot.system.hardware import PC
  from openpilot.system.hardware.hw import Paths

  return Path(Paths.comma_home()) / "vehicle_telemetry" if PC else Path("/data/vehicle_telemetry")


def vehicle_telemetry_dir() -> Path:
  return Path(_data_dir_provider()) if _data_dir_provider is not None else _default_vehicle_telemetry_dir()


def vehicle_telemetry_config_path() -> Path:
  return vehicle_telemetry_dir() / VEHICLE_TELEMETRY_CONFIG_FILENAME


def vehicle_telemetry_cache_path() -> Path:
  return vehicle_telemetry_dir() / VEHICLE_TELEMETRY_CACHE_FILENAME


def vehicle_telemetry_status_path() -> Path:
  return vehicle_telemetry_dir() / VEHICLE_TELEMETRY_STATUS_FILENAME


def load_vehicle_telemetry_status(path=None):
  status_path = Path(path) if path is not None else vehicle_telemetry_status_path()
  return _read_owner_only_json(status_path) or {}


def _finite_float(value, default=float("nan")):
  try:
    result = float(value)
  except (TypeError, ValueError):
    return default
  return result if math.isfinite(result) else default


def _bounded_interval(value, default, minimum, maximum=3600.0):
  parsed = _finite_float(value, default)
  return max(minimum, min(maximum, parsed))


def _bounded_int(value, default, minimum, maximum):
  try:
    parsed = int(value)
  except (TypeError, ValueError):
    parsed = default
  return max(minimum, min(maximum, parsed))


def _atomic_write_bytes(path: Path, payload: bytes, mode=0o600, durable=True):
  path = Path(path)
  path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
  try:
    path.parent.chmod(0o700)
  except OSError:
    pass

  temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
  descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
  try:
    with os.fdopen(descriptor, "wb") as output:
      output.write(payload)
      output.flush()
      if durable:
        os.fsync(output.fileno())
    os.chmod(temp_path, mode)
    os.replace(temp_path, path)
  finally:
    try:
      temp_path.unlink()
    except FileNotFoundError:
      pass


def _atomic_write_json(path: Path, payload: dict, mode=0o600, durable=True):
  encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
  _atomic_write_bytes(Path(path), encoded, mode=mode, durable=durable)


def _read_owner_only_json(path: Path):
  try:
    file_stat = path.stat()
    if file_stat.st_uid != os.geteuid() or file_stat.st_mode & 0o077:
      return None
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else None
  except Exception:
    return None


def _valid_https_url(value):
  url = str(value or "").strip()
  if not url or len(url) > 2048:
    return ""
  parsed = urlsplit(url)
  if parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.fragment:
    return ""
  return url


def _valid_host(value):
  host = str(value or "").strip().lower().rstrip(".")
  if not host or len(host) > 253 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host):
    return ""
  if ".." in host:
    return ""
  return host


def _valid_bind_address(value):
  address = str(value or "").strip()
  if address in ("localhost", "0.0.0.0", "::", "127.0.0.1", "::1"):
    return address
  # Binding a user-supplied interface address is safe; reject punctuation that
  # could make diagnostics or generated configuration ambiguous.
  return address if re.fullmatch(r"[0-9a-fA-F:.%]+", address) else "127.0.0.1"


def _valid_subdomain(value):
  subdomain = str(value or "auto").strip().lower()
  if subdomain == "auto":
    return subdomain
  return subdomain if re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", subdomain) else "auto"


def _valid_absolute_path(value, default):
  path = str(value or default).strip()[:1024]
  return path if path.startswith("/") and "\x00" not in path else default


def default_vehicle_telemetry_config():
  return {
    "schemaVersion": VEHICLE_TELEMETRY_SCHEMA_VERSION,
    "mode": "off",
    "fetch": {
      "enabled": False,
      "token": "",
      "clients": [],
      "bindAddress": "127.0.0.1",
      "port": VEHICLE_TELEMETRY_DEFAULT_PORT,
    },
    "push": {
      "enabled": False,
      "url": "",
      "token": "",
      "vehicleId": "",
      "vehicleName": "",
      "maximumBatteryCapacityKilowattHours": None,
      "drivingIntervalSeconds": 60.0,
      "chargingIntervalSeconds": 120.0,
      "parkedIntervalSeconds": 900.0,
    },
    "tunnel": {
      "provider": "frp",
      "binaryPath": "/data/vehicle_telemetry/bin/frpc",
      "serverAddress": "",
      "serverPort": 7000,
      "token": "",
      "subdomainHost": "",
      "subdomain": "auto",
      "trustedCaFile": "",
      "serverName": "",
    },
    "tailscale": {
      "binaryPath": "/data/tailscale/tailscale",
      "daemonBinaryPath": "/data/tailscale/tailscaled",
      "socketPath": "/data/tailscale/tailscaled.sock",
      "stateDirectory": "/data/tailscale/state",
      "hostname": "auto",
      "httpsPort": 443,
    },
  }


def _normalize_vehicle_telemetry_config(raw):
  config = default_vehicle_telemetry_config()
  if not isinstance(raw, dict):
    return config

  fetch_raw = raw.get("fetch") if isinstance(raw.get("fetch"), dict) else {}
  push_raw = raw.get("push") if isinstance(raw.get("push"), dict) else {}
  tunnel_raw = raw.get("tunnel") if isinstance(raw.get("tunnel"), dict) else {}
  tailscale_raw = raw.get("tailscale") if isinstance(raw.get("tailscale"), dict) else {}
  requested_mode = str(raw.get("mode") or "").strip().lower()
  if not requested_mode:
    requested_mode = _legacy_fetch_mode if fetch_raw.get("enabled") else "off"
  config["mode"] = requested_mode if requested_mode in TELEMETRY_MODES else "off"

  fetch_token = str(fetch_raw.get("token") or "").strip()
  fetch_clients = []
  raw_clients = fetch_raw.get("clients") if isinstance(fetch_raw.get("clients"), list) else []
  for raw_client in raw_clients[:8]:
    if not isinstance(raw_client, dict):
      continue
    client_token = str(raw_client.get("token") or "").strip()
    if len(client_token) < 32 or any(client["token"] == client_token for client in fetch_clients):
      continue
    fetch_clients.append(
      {
        "name": str(raw_client.get("name") or "External app").strip()[:80] or "External app",
        "token": client_token,
        "createdAt": _finite_float(raw_client.get("createdAt"), 0.0),
      }
    )
  config["fetch"] = {
    "enabled": bool(fetch_raw.get("enabled", False)) and (len(fetch_token) >= 32 or bool(fetch_clients)),
    "token": fetch_token if len(fetch_token) >= 32 else "",
    "clients": fetch_clients,
    "bindAddress": _valid_bind_address(fetch_raw.get("bindAddress") or "127.0.0.1"),
    "port": _bounded_int(fetch_raw.get("port"), VEHICLE_TELEMETRY_DEFAULT_PORT, 1024, 65535),
  }
  if config["mode"] in ("off", "send"):
    config["fetch"]["enabled"] = False

  push_token = str(push_raw.get("token") or "").strip()
  push_url = _valid_https_url(push_raw.get("url"))
  battery_capacity = _finite_float(push_raw.get("maximumBatteryCapacityKilowattHours"), 0.0)
  config["push"] = {
    "enabled": bool(push_raw.get("enabled", False)) and bool(push_url and len(push_token) >= 32),
    "url": push_url,
    "token": push_token if len(push_token) >= 32 else "",
    "vehicleId": str(push_raw.get("vehicleId") or "").strip()[:128],
    "vehicleName": str(push_raw.get("vehicleName") or "").strip()[:120],
    "maximumBatteryCapacityKilowattHours": battery_capacity if battery_capacity > 0.0 else None,
    "drivingIntervalSeconds": _bounded_interval(push_raw.get("drivingIntervalSeconds"), 60.0, 30.0),
    "chargingIntervalSeconds": _bounded_interval(push_raw.get("chargingIntervalSeconds"), 120.0, 60.0),
    "parkedIntervalSeconds": _bounded_interval(push_raw.get("parkedIntervalSeconds"), 900.0, 300.0),
  }

  tunnel_token = str(tunnel_raw.get("token") or "").strip()
  trusted_ca_file = str(tunnel_raw.get("trustedCaFile") or "").strip()[:1024]
  binary_path = str(tunnel_raw.get("binaryPath") or config["tunnel"]["binaryPath"]).strip()[:1024]
  config["tunnel"] = {
    "provider": "frp",
    "binaryPath": binary_path if binary_path.startswith("/") else config["tunnel"]["binaryPath"],
    "serverAddress": _valid_host(tunnel_raw.get("serverAddress")),
    "serverPort": _bounded_int(tunnel_raw.get("serverPort"), 7000, 1, 65535),
    "token": tunnel_token if len(tunnel_token) >= 32 else "",
    "subdomainHost": _valid_host(tunnel_raw.get("subdomainHost")),
    "subdomain": _valid_subdomain(tunnel_raw.get("subdomain")),
    "trustedCaFile": trusted_ca_file if trusted_ca_file.startswith("/") else "",
    "serverName": _valid_host(tunnel_raw.get("serverName")),
  }
  tailscale_default = config["tailscale"]
  config["tailscale"] = {
    "binaryPath": _valid_absolute_path(tailscale_raw.get("binaryPath"), tailscale_default["binaryPath"]),
    "daemonBinaryPath": _valid_absolute_path(tailscale_raw.get("daemonBinaryPath"), tailscale_default["daemonBinaryPath"]),
    "socketPath": _valid_absolute_path(tailscale_raw.get("socketPath"), tailscale_default["socketPath"]),
    "stateDirectory": _valid_absolute_path(tailscale_raw.get("stateDirectory"), tailscale_default["stateDirectory"]),
    "hostname": _valid_subdomain(tailscale_raw.get("hostname")),
    "httpsPort": _bounded_int(tailscale_raw.get("httpsPort"), 443, 443, 443),
  }
  return config


def _load_legacy_push_config(path: Path):
  raw = _read_owner_only_json(path)
  if raw is None:
    return None
  return {
    "schemaVersion": VEHICLE_TELEMETRY_SCHEMA_VERSION,
    "mode": "off",
    "fetch": {"enabled": False, "token": ""},
    "push": {"enabled": True, **raw},
  }


def load_vehicle_telemetry_config(path=None):
  config_path = Path(path) if path is not None else vehicle_telemetry_config_path()
  raw = _read_owner_only_json(config_path)
  if raw is None and path is None:
    legacy_combined = _read_owner_only_json(vehicle_telemetry_dir() / VEHICLE_TELEMETRY_LEGACY_COMBINED_CONFIG_FILENAME)
    if isinstance(legacy_combined, dict) and (isinstance(legacy_combined.get("fetch"), dict) or isinstance(legacy_combined.get("push"), dict)):
      raw = legacy_combined
  if raw is None and path is None:
    raw = _load_legacy_push_config(vehicle_telemetry_dir() / VEHICLE_TELEMETRY_LEGACY_CONFIG_FILENAME)
  return _normalize_vehicle_telemetry_config(raw)


class VehicleTelemetryConfigLoader:
  """Share a normalized config for a few seconds to bound idle disk reads."""

  def __init__(self, path=None, reload_seconds=VEHICLE_TELEMETRY_CONFIG_RELOAD_SECONDS, *, monotonic=None, loader=None):
    self.path = Path(path) if path is not None else None
    self.reload_seconds = max(0.0, float(reload_seconds))
    self.monotonic = monotonic or time.monotonic
    self.loader = loader or load_vehicle_telemetry_config
    self._lock = threading.Lock()
    self._next_load = float("-inf")
    self._config = None

  def get(self):
    now = self.monotonic()
    with self._lock:
      if self._config is None or now >= self._next_load:
        self._config = self.loader(self.path) if self.path is not None else self.loader()
        self._next_load = now + self.reload_seconds
      return self._config


def save_vehicle_telemetry_config(raw, path=None):
  config_path = Path(path) if path is not None else vehicle_telemetry_config_path()
  config = _normalize_vehicle_telemetry_config(raw)
  _atomic_write_json(config_path, config)
  return config


def public_vehicle_telemetry_config(config):
  parsed = _normalize_vehicle_telemetry_config(config)
  fetch = parsed["fetch"]
  push = parsed["push"]
  tunnel = parsed["tunnel"]
  tailscale = parsed["tailscale"]
  return {
    "schemaVersion": VEHICLE_TELEMETRY_SCHEMA_VERSION,
    "mode": parsed["mode"],
    "fetch": {
      "enabled": fetch["enabled"],
      "hasToken": bool(fetch["token"]),
      "pairedClientCount": len(fetch["clients"]),
      "bindAddress": fetch["bindAddress"],
      "port": fetch["port"],
    },
    "push": {key: value for key, value in push.items() if key != "token"} | {"hasToken": bool(push["token"])},
    "tunnel": {key: value for key, value in tunnel.items() if key != "token"} | {"hasToken": bool(tunnel["token"])},
    "tailscale": tailscale,
  }


def is_fetch_authorized(config, authorization_header):
  fetch = _normalize_vehicle_telemetry_config(config)["fetch"]
  if not fetch["enabled"]:
    return False
  supplied = str(authorization_header or "")
  prefix = "Bearer "
  if not supplied.startswith(prefix):
    return False
  token = supplied[len(prefix) :].strip()
  expected_tokens = [fetch["token"], *(client["token"] for client in fetch["clients"])]
  return any(expected and hmac.compare_digest(token, expected) for expected in expected_tokens)


def build_vehicle_telemetry_snapshot(car_state, timestamp=None, vehicle_fingerprint="", source_name=None):
  """Normalize validated generic CarState fields into the transport schema."""
  fuel_gauge = _finite_float(getattr(car_state, "fuelGauge", None))
  distance_to_empty_meters = _finite_float(getattr(car_state, "distanceToEmpty", None))
  has_soc = math.isfinite(fuel_gauge) and 0.0 <= fuel_gauge <= 1.0
  has_dte = math.isfinite(distance_to_empty_meters) and 0.0 < distance_to_empty_meters < 900000.0

  # CarState scalar defaults are zero. Requiring at least one non-zero energy
  # value prevents an unsupported or not-yet-observed vehicle looking valid.
  if not has_dte and (not has_soc or fuel_gauge <= 0.0):
    return None

  distance_to_empty_km = round(distance_to_empty_meters / 1000.0, 1) if has_dte else None
  payload = {
    "schemaVersion": VEHICLE_TELEMETRY_SCHEMA_VERSION,
    "source": str(source_name or _source_name)[:80],
    "updatedAt": time.time() if timestamp is None else float(timestamp),  # noqa: TID251
    "vehicleFingerprint": str(vehicle_fingerprint or "")[:160],
    "stateOfChargePercent": round(max(0.0, min(100.0, fuel_gauge * 100.0)), 1) if has_soc else None,
    "distanceToEmptyKilometers": distance_to_empty_km,
    "isCharging": bool(getattr(car_state, "charging", False)),
    "isPluggedIn": bool(getattr(car_state, "chargingPortConnected", False)),
    "speedMetersPerSecond": round(max(0.0, _finite_float(getattr(car_state, "vEgo", 0.0), 0.0)), 3),
    "standstill": bool(getattr(car_state, "standstill", False)),
  }
  return {key: value for key, value in payload.items() if value is not None}


def vehicle_telemetry_activity(payload, is_onroad=None):
  if payload.get("isCharging") is True:
    return "charging"
  if is_onroad is not None:
    return "driving" if is_onroad else "parked"
  if abs(_finite_float(payload.get("speedMetersPerSecond"), 0.0)) >= 0.5:
    return "driving"
  return "parked"


def _vehicle_telemetry_signature(payload):
  return tuple(payload.get(key) for key in _TELEMETRY_FIELDS if key != "updatedAt")


def telemetry_response(snapshot, now=None, vehicle_id=""):
  if not isinstance(snapshot, dict):
    return None
  timestamp = time.time() if now is None else float(now)  # noqa: TID251
  updated_at = _finite_float(snapshot.get("updatedAt"), 0.0)
  age_seconds = max(0.0, timestamp - updated_at) if updated_at > 0.0 else None
  is_live = age_seconds is not None and age_seconds <= VEHICLE_TELEMETRY_LIVE_SECONDS
  response = {
    **snapshot,
    "availability": "live" if is_live else "cached",
    "isLive": is_live,
    "ageSeconds": round(age_seconds, 1) if age_seconds is not None else None,
  }
  identifier = str(vehicle_id or "").strip()[:128]
  if identifier:
    response["vehicleId"] = identifier
    if re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", identifier, re.IGNORECASE):
      response["vin"] = identifier.upper()
  return response


class VehicleTelemetryCache:
  def __init__(self, path=None, heartbeat_seconds=VEHICLE_TELEMETRY_HEARTBEAT_SECONDS):
    self.path = Path(path) if path is not None else vehicle_telemetry_cache_path()
    self.heartbeat_seconds = float(heartbeat_seconds)
    self.latest = self.load()
    self._last_write_monotonic = 0.0
    self._last_signature = _vehicle_telemetry_signature(self.latest) if self.latest else None

  def load(self):
    snapshot = _read_owner_only_json(self.path)
    if (
      isinstance(snapshot, dict)
      and snapshot.get("estimatedRangeKilometers") == snapshot.get("distanceToEmptyKilometers")
      and snapshot.get("distanceToEmptyKilometers") is not None
    ):
      snapshot = dict(snapshot)
      snapshot.pop("estimatedRangeKilometers", None)
    return snapshot

  def store(self, snapshot, monotonic_now=None):
    if not isinstance(snapshot, dict):
      return False
    signature = _vehicle_telemetry_signature(snapshot)
    now_mono = time.monotonic() if monotonic_now is None else float(monotonic_now)
    heartbeat_due = now_mono - self._last_write_monotonic >= self.heartbeat_seconds
    self.latest = dict(snapshot)
    if signature == self._last_signature and not heartbeat_due:
      return False
    _atomic_write_json(self.path, self.latest, durable=False)
    self._last_signature = signature
    self._last_write_monotonic = now_mono
    return True


class VehicleTelemetryPublisher:
  """Low-bandwidth HTTPS publisher for the latest normalized snapshot."""

  def __init__(self, config_path=None, status_path=None, session=None):
    self.config_path = Path(config_path) if config_path is not None else None
    self.status_path = Path(status_path) if status_path is not None else vehicle_telemetry_status_path()
    self.session = session or requests.Session()
    self.session.trust_env = False
    self.config_loader = VehicleTelemetryConfigLoader(self.config_path)
    self._condition = threading.Condition()
    self._latest = None
    self._is_onroad = None
    self._thread = None

  def start(self):
    with self._condition:
      if self._thread is not None and self._thread.is_alive():
        return
      self._thread = threading.Thread(target=self._worker, name="vehicle-telemetry-publisher", daemon=True)
      self._thread.start()

  def submit(self, snapshot):
    if not isinstance(snapshot, dict):
      return
    compact = {key: snapshot[key] for key in ("schemaVersion", *_TELEMETRY_FIELDS) if snapshot.get(key) is not None}
    with self._condition:
      previous = self._latest
      self._latest = compact
      if previous is None or _vehicle_telemetry_signature(previous) != _vehicle_telemetry_signature(compact):
        self._condition.notify()

  def set_onroad(self, is_onroad):
    with self._condition:
      previous = self._is_onroad
      self._is_onroad = bool(is_onroad)
      if previous is None or previous != self._is_onroad:
        self._condition.notify()

  def _config(self):
    return self.config_loader.get()

  def _wait(self, timeout):
    with self._condition:
      self._condition.wait(timeout=max(1.0, min(float(timeout), 30.0)))

  def _post(self, push_config, snapshot):
    telemetry = dict(snapshot)
    if push_config["vehicleName"]:
      telemetry["vehicleName"] = push_config["vehicleName"]
    if push_config["maximumBatteryCapacityKilowattHours"] is not None:
      telemetry["maximumBatteryCapacityKilowattHours"] = push_config["maximumBatteryCapacityKilowattHours"]

    response = None
    try:
      response = self.session.post(
        push_config["url"],
        headers={"Authorization": f"Bearer {push_config['token']}", "Content-Type": "application/json"},
        json={
          "schemaVersion": VEHICLE_TELEMETRY_SCHEMA_VERSION,
          "vehicleId": push_config["vehicleId"],
          "sentAt": int(time.time() * 1000),  # noqa: TID251
          "telemetry": telemetry,
        },
        timeout=(3.0, 5.0),
        allow_redirects=False,
      )
      return 200 <= response.status_code < 300, response.status_code
    except Exception:
      return False, None
    finally:
      if response is not None:
        response.close()

  def _write_status(self, success, status_code, push_config, activity):
    parsed_url = urlsplit(push_config.get("url") or "")
    _atomic_write_json(
      self.status_path,
      {
        "schemaVersion": VEHICLE_TELEMETRY_SCHEMA_VERSION,
        "updatedAt": time.time(),  # noqa: TID251
        "success": bool(success),
        "statusCode": status_code,
        "activity": activity,
        "endpointHost": parsed_url.hostname or "",
      },
    )

  def _worker(self):
    last_success_mono = 0.0
    last_attempt_mono = 0.0
    last_activity = None
    last_signature = None
    backoff_seconds = 0.0

    while True:
      push = self._config()["push"]
      with self._condition:
        snapshot = dict(self._latest) if self._latest is not None else None
        is_onroad = self._is_onroad
      if not push["enabled"] or snapshot is None:
        self._wait(30.0 if not push["enabled"] else 5.0)
        continue

      now_mono = time.monotonic()
      activity = vehicle_telemetry_activity(snapshot, is_onroad)
      signature = _vehicle_telemetry_signature(snapshot)
      interval = push[f"{activity}IntervalSeconds"]
      first_push = last_activity is None
      transition = not first_push and activity != last_activity
      meaningful_change = signature != last_signature
      periodic_due = now_mono - last_success_mono >= interval
      should_push = first_push or transition or periodic_due or (activity == "parked" and meaningful_change)
      retry_remaining = max(0.0, backoff_seconds - (now_mono - last_attempt_mono))

      if not should_push or retry_remaining > 0.0:
        self._wait(retry_remaining if retry_remaining > 0.0 else min(interval, 30.0))
        continue

      last_attempt_mono = now_mono
      success, status_code = self._post(push, snapshot)
      self._write_status(success, status_code, push, activity)
      if success:
        last_success_mono = now_mono
        last_activity = activity
        last_signature = signature
        backoff_seconds = 0.0
      else:
        backoff_seconds = min(300.0, max(15.0, backoff_seconds * 2.0))
