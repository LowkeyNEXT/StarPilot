#!/usr/bin/env python3
"""Read-only ELM327/ST virtual adapter backed by validated vehicle telemetry.

This module deliberately contains no Panda, CAN, or cereal messaging imports.
It turns an immutable normalized telemetry snapshot into diagnostic responses;
the BlueZ frontend is only a serial transport for this state machine.
"""

from __future__ import annotations

import re
import threading
import time

from collections.abc import Callable
from dataclasses import dataclass

from openpilot.system.vehicle_telemetry.core import VEHICLE_TELEMETRY_LIVE_SECONDS, validated_vehicle_telemetry_snapshot


OBD_UART_SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
OBD_UART_NOTIFY_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"
OBD_UART_WRITE_UUID = "0000fff2-0000-1000-8000-00805f9b34fb"
ELM_UART_SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
ELM_UART_CHARACTERISTIC_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"
OBD_UART_SERVICE_UUIDS = (OBD_UART_SERVICE_UUID, ELM_UART_SERVICE_UUID)
DEFAULT_OBD_BLE_NAME = "CommaOBD"
LEGACY_OBD_BLE_NAME = "StarPilot OBD"
MAX_OBD_BLE_NAME_BYTES = 24

ELM_IDENTITY = "ELM327 v1.4b"
ST_IDENTITY = "STN2230 v5.6.19"
STARPILOT_ADAPTER_DESCRIPTION = "CommaOBD secure virtual ELM327"

EV_REQUEST_HEADER = 0x7E4
EV_RESPONSE_HEADER = 0x7EC
FUNCTIONAL_REQUEST_HEADER = 0x7DF
GENERIC_RESPONSE_HEADER = 0x7E8
STARPILOT_REQUEST_HEADER = 0x7E6
STARPILOT_RESPONSE_HEADER = 0x7EE

STARPILOT_DID_STATUS = 0xD100
STARPILOT_DID_SOC = 0xD101
STARPILOT_DID_DISTANCE_TO_EMPTY = 0xD102
STARPILOT_DID_SPEED = 0xD103
STARPILOT_DID_MINUTES_TO_FULL = 0xD104
STARPILOT_DID_COLLECTED_AT = 0xD105

STARPILOT_CACHED_TELEMETRY_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
HKMC_BMS_PAGE_101 = 0x0101
HKMC_BMS_PAGE_105 = 0x0105

MAX_COMMAND_BUFFER_BYTES = 512

_HEX_RE = re.compile(r"^[0-9A-F]+$")
_HEADER_RE = re.compile(r"^[0-9A-F]{3}$")


def normalize_obd_ble_name(value) -> str:
  if isinstance(value, bytes):
    value = value.decode("utf-8", errors="ignore")
  name = " ".join(str(value or "").split())
  name = "".join(character for character in name if character.isprintable())
  while len(name.encode("utf-8")) > MAX_OBD_BLE_NAME_BYTES:
    name = name[:-1]
  if not name or name.casefold() == LEGACY_OBD_BLE_NAME.casefold():
    return DEFAULT_OBD_BLE_NAME
  return name


def _bounded_int(value, minimum, maximum, default=0):
  try:
    parsed = int(round(float(value)))
  except (TypeError, ValueError, OverflowError):
    return default
  return max(minimum, min(maximum, parsed))


def _u16(value):
  return _bounded_int(value, 0, 0xFFFF).to_bytes(2, "big")


def _s16(value):
  return _bounded_int(value, -0x8000, 0x7FFF).to_bytes(2, "big", signed=True)


def _u32(value):
  return _bounded_int(value, 0, 0xFFFFFFFF).to_bytes(4, "big")


def _supported_pid_mask(start_pid, supported):
  mask = 0
  for pid in supported:
    offset = pid - start_pid
    if 1 <= offset <= 32:
      mask |= 1 << (32 - offset)
  return mask.to_bytes(4, "big")


def _iso_tp_frames(payload: bytes) -> list[bytes]:
  if len(payload) <= 7:
    return [bytes([len(payload)]) + payload]
  if len(payload) > 0xFFF:
    raise ValueError("ISO-TP payload is too large")
  frames = [bytes([0x10 | (len(payload) >> 8), len(payload) & 0xFF]) + payload[:6]]
  offset = 6
  sequence = 1
  while offset < len(payload):
    frames.append(bytes([0x20 | (sequence & 0x0F)]) + payload[offset:offset + 7])
    offset += 7
    sequence = (sequence + 1) & 0x0F
  return frames


@dataclass(frozen=True)
class DiagnosticResponse:
  header: int
  payload: bytes


class VirtualObdVehicle:
  """Maps safe read-only diagnostic requests to a normalized snapshot."""

  STANDARD_PIDS = frozenset({0x0D, 0x1C, 0x5B})

  def __init__(self, snapshot_provider: Callable[[], dict | None], *, clock: Callable[[], float] | None = None):
    self._snapshot_provider = snapshot_provider
    # Snapshot timestamps are Unix wall-clock seconds, so monotonic time cannot
    # be used for freshness checks here.
    self._clock = clock or time.time  # noqa: TID251

  def _snapshot(self, maximum_age_seconds):
    raw = self._snapshot_provider()
    snapshot = validated_vehicle_telemetry_snapshot(raw, now=self._clock())
    if snapshot is None:
      return None
    age = self._clock() - float(snapshot["updatedAt"])
    return snapshot if 0.0 <= age <= maximum_age_seconds else None

  def _live_snapshot(self):
    return self._snapshot(VEHICLE_TELEMETRY_LIVE_SECONDS)

  def _cached_snapshot(self):
    return self._snapshot(STARPILOT_CACHED_TELEMETRY_MAX_AGE_SECONDS)

  def _service_01(self, pid: int, snapshot: dict | None):
    if pid in (0x00, 0x20, 0x40, 0x60, 0x80, 0xA0):
      supported = set(self.STANDARD_PIDS)
      # SAE J1979 range queries use their last bit to advertise the next
      # supported range. Without this chain, clients stop before reaching 5B.
      range_end = pid + 0x20
      if any(supported_pid > range_end for supported_pid in supported):
        supported.add(range_end)
      if pid > max(self.STANDARD_PIDS):
        return None
      return bytes([0x41, pid]) + _supported_pid_mask(pid, supported)

    if pid == 0x0D and snapshot is not None and "speedMetersPerSecond" in snapshot:
      speed_kph = _bounded_int(float(snapshot["speedMetersPerSecond"]) * 3.6, 0, 255)
      return bytes([0x41, pid, speed_kph])
    if pid == 0x1C:
      # OBD-II as defined by the US OBD-II requirements. This is an adapter
      # compatibility response, not a claim about a combustion powertrain.
      return bytes([0x41, pid, 0x01])
    if pid == 0x5B and snapshot is not None and "stateOfChargePercent" in snapshot:
      raw_soc = _bounded_int(float(snapshot["stateOfChargePercent"]) * 255.0 / 100.0, 0, 255)
      return bytes([0x41, pid, raw_soc])
    return None

  def _starpilot_did(self, did: int, snapshot: dict | None):
    if snapshot is None:
      return None
    if did == STARPILOT_DID_STATUS:
      flags = 0
      flags |= int(snapshot.get("isCharging") is True) << 0
      flags |= int(snapshot.get("isPluggedIn") is True) << 1
      flags |= int(snapshot.get("standstill") is True) << 2
      age_seconds = _bounded_int(self._clock() - float(snapshot["updatedAt"]), 0, 0xFFFF)
      return bytes([0x62, did >> 8, did & 0xFF, 1, flags]) + age_seconds.to_bytes(2, "big")
    if did == STARPILOT_DID_SOC and "stateOfChargePercent" in snapshot:
      return bytes([0x62, did >> 8, did & 0xFF]) + _u16(float(snapshot["stateOfChargePercent"]) * 10.0)
    if did == STARPILOT_DID_DISTANCE_TO_EMPTY and "distanceToEmptyKilometers" in snapshot:
      return bytes([0x62, did >> 8, did & 0xFF]) + _u16(float(snapshot["distanceToEmptyKilometers"]) * 10.0)
    if did == STARPILOT_DID_SPEED and "speedMetersPerSecond" in snapshot:
      return bytes([0x62, did >> 8, did & 0xFF]) + _u16(float(snapshot["speedMetersPerSecond"]) * 100.0)
    if did == STARPILOT_DID_MINUTES_TO_FULL and "minutesToFull" in snapshot:
      return bytes([0x62, did >> 8, did & 0xFF]) + _u16(snapshot["minutesToFull"])
    if did == STARPILOT_DID_COLLECTED_AT:
      return bytes([0x62, did >> 8, did & 0xFF]) + _u32(snapshot["updatedAt"])
    return None

  @staticmethod
  def _hkmc_bms_page(did: int, snapshot: dict | None):
    if snapshot is None or "stateOfChargePercent" not in snapshot:
      return None

    # ABRP's Hyundai/Kia profile reads these standard BMS pages. Populate only
    # normalized fields StarPilot has validated; never forward phone commands
    # to CAN or fabricate unavailable vehicle measurements.
    raw_soc = _bounded_int(float(snapshot["stateOfChargePercent"]) * 2.0, 0, 200)
    if did == HKMC_BMS_PAGE_101:
      page = bytearray(59)
      page[4] = raw_soc
      page[9] = (0x80 if snapshot.get("isCharging") is True else 0) | (0x20 if snapshot.get("isPluggedIn") is True else 0)
      page[14:20] = b"\x80" * 6
      page[22] = 0x80
      if "batteryCurrentAmps" in snapshot:
        page[10:12] = _s16(float(snapshot["batteryCurrentAmps"]) * 10.0)
      if "batteryVoltageVolts" in snapshot:
        page[12:14] = _u16(float(snapshot["batteryVoltageVolts"]) * 10.0)
      minimum_temperature = snapshot.get("minimumBatteryTemperatureCelsius")
      maximum_temperature = snapshot.get("maximumBatteryTemperatureCelsius")
      if minimum_temperature is not None and maximum_temperature is not None:
        minimum_raw = _bounded_int(minimum_temperature, -40, 100) & 0xFF
        maximum_raw = _bounded_int(maximum_temperature, -40, 100) & 0xFF
        page[14:20] = bytes([maximum_raw, minimum_raw, maximum_raw, minimum_raw, maximum_raw, minimum_raw])
      return bytes([0x62, 0x01, 0x01]) + page
    if did == HKMC_BMS_PAGE_105:
      page = bytearray(45)
      page[8:16] = b"\x80" * 8
      page[39:43] = b"\x80" * 4
      if "remainingEnergyKilowattHours" in snapshot:
        page[28:30] = _u16(float(snapshot["remainingEnergyKilowattHours"]) * 500.0)
      page[31] = raw_soc
      return bytes([0x62, 0x01, 0x05]) + page
    return None

  def respond(self, request_header: int, request: bytes) -> list[DiagnosticResponse]:
    if not request:
      return []
    service = request[0]

    if service == 0x01 and len(request) == 2:
      snapshot = self._live_snapshot()
      payload = self._service_01(request[1], snapshot)
      if payload is None:
        return []
      response_header = EV_RESPONSE_HEADER if request_header == EV_REQUEST_HEADER else GENERIC_RESPONSE_HEADER
      return [DiagnosticResponse(response_header, payload)]

    if service == 0x22 and len(request) == 3 and request_header == STARPILOT_REQUEST_HEADER:
      snapshot = self._cached_snapshot()
      did = int.from_bytes(request[1:], "big")
      payload = self._starpilot_did(did, snapshot) if snapshot is not None else None
      return [DiagnosticResponse(STARPILOT_RESPONSE_HEADER, payload)] if payload is not None else []

    if service == 0x22 and len(request) == 3 and request_header in (FUNCTIONAL_REQUEST_HEADER, EV_REQUEST_HEADER):
      did = int.from_bytes(request[1:], "big")
      payload = self._hkmc_bms_page(did, self._live_snapshot())
      return [DiagnosticResponse(EV_RESPONSE_HEADER, payload)] if payload is not None else []

    # No write-capable UDS/OBD service is implemented. Unknown reads and every
    # diagnostic-session, actuator, coding, clearing, or transfer command end
    # here and can never reach a vehicle transport.
    return []


class Elm327Session:
  """Incremental ELM327-compatible command stream for one BLE central."""

  def __init__(self, vehicle: VirtualObdVehicle):
    self.vehicle = vehicle
    self._buffer = bytearray()
    self.last_command = ""
    self.reset()

  def reset(self):
    self.echo = True
    self.linefeeds = True
    self.spaces = True
    self.headers = False
    self.can_auto_format = True
    self.protocol = "AUTO"
    self.request_header = FUNCTIONAL_REQUEST_HEADER
    self.receive_header: int | None = None

  @property
  def _line_end(self):
    return "\r\n" if self.linefeeds else "\r"

  def _format_bytes(self, data: bytes):
    separator = " " if self.spaces else ""
    return separator.join(f"{value:02X}" for value in data)

  def _format_diagnostic(self, response: DiagnosticResponse):
    if not self.headers and self.can_auto_format:
      return self._format_bytes(response.payload)
    frames = _iso_tp_frames(response.payload)
    lines = []
    for frame in frames:
      body = self._format_bytes(frame)
      if self.headers:
        separator = " " if self.spaces else ""
        body = f"{response.header:03X}{separator}{body}"
      lines.append(body)
    return self._line_end.join(lines)

  def _set_boolean(self, command: str, prefix: str, attribute: str):
    if not command.startswith(prefix):
      return None
    value = command[len(prefix):]
    if value not in ("0", "1"):
      return None
    setattr(self, attribute, value == "1")
    return "OK"

  def _handle_at(self, command: str):
    compact = command.replace(" ", "")
    if compact in ("ATZ", "ATWS"):
      self.reset()
      return ELM_IDENTITY
    if compact == "ATD":
      self.reset()
      return "OK"
    if compact == "ATI":
      return ELM_IDENTITY
    if compact == "AT@1":
      return STARPILOT_ADAPTER_DESCRIPTION
    if compact == "AT@2":
      return DEFAULT_OBD_BLE_NAME
    if compact == "ATDP":
      return "AUTO, ISO 15765-4 (CAN 11/500)" if self.protocol == "AUTO" else "ISO 15765-4 (CAN 11/500)"
    if compact == "ATDPN":
      return "A6" if self.protocol == "AUTO" else "6"
    if compact == "ATRV":
      return "12.0V"
    if compact == "ATIGN":
      return "ON"

    for prefix, attribute in (
      ("ATE", "echo"),
      ("ATL", "linefeeds"),
      ("ATS", "spaces"),
      ("ATH", "headers"),
      ("ATCAF", "can_auto_format"),
    ):
      result = self._set_boolean(compact, prefix, attribute)
      if result is not None:
        return result

    if compact.startswith("ATSP"):
      value = compact[4:]
      if value in ("0", "00"):
        self.protocol = "AUTO"
        return "OK"
      if value == "6":
        self.protocol = "6"
        return "OK"
      return "?"
    if compact.startswith("ATSH"):
      value = compact[4:]
      if not _HEADER_RE.fullmatch(value):
        return "?"
      self.request_header = int(value, 16)
      return "OK"
    if compact.startswith("ATCRA"):
      value = compact[5:]
      if value == "":
        self.receive_header = None
        return "OK"
      if not _HEADER_RE.fullmatch(value):
        return "?"
      self.receive_header = int(value, 16)
      return "OK"

    # Safe adapter-local tuning commands that do not affect response meaning.
    if compact in ("ATAL", "ATAT0", "ATAT1", "ATAT2", "ATCFC0", "ATCFC1", "ATFE", "ATPC"):
      return "OK"
    if compact.startswith("ATST") and len(compact) == 6 and _HEX_RE.fullmatch(compact[4:]):
      return "OK"
    return "?"

  def _handle_st(self, command: str):
    compact = command.replace(" ", "")
    if compact == "STI":
      return ST_IDENTITY
    if compact == "STDI":
      return STARPILOT_ADAPTER_DESCRIPTION
    if compact.startswith("STPX"):
      return self._handle_stpx(compact[4:])
    # Do not claim support for monitoring, periodic messaging, or other raw-CAN
    # ST commands. Unlike STPX below, they cannot be constrained to one
    # validated request and response transaction.
    return "?"

  def _handle_stpx(self, raw_parameters: str):
    parameters = {}
    for raw_parameter in raw_parameters.lstrip(",").split(","):
      if not raw_parameter or ":" not in raw_parameter:
        return "?"
      name, value = raw_parameter.split(":", 1)
      if name not in ("H", "D", "R", "T", "X", "F") or name in parameters:
        return "?"
      parameters[name] = value

    # Length/DATA> mode is intentionally unsupported. Requiring the full data
    # in this one command makes it possible to validate the entire read query
    # before handing it to the virtual vehicle.
    data = parameters.get("D")
    if data is None or len(data) < 4 or len(data) % 2 or not _HEX_RE.fullmatch(data):
      return "?"
    header_text = parameters.get("H", f"{self.request_header:03X}")
    if not _HEADER_RE.fullmatch(header_text):
      return "?"
    for numeric_name in ("R", "T", "X", "F"):
      value = parameters.get(numeric_name)
      if value is not None and (not value or not _HEX_RE.fullmatch(value)):
        return "?"

    responses = self.vehicle.respond(int(header_text, 16), bytes.fromhex(data))
    if self.receive_header is not None:
      responses = [response for response in responses if response.header == self.receive_header]
    if not responses:
      return "NO DATA"
    return self._line_end.join(self._format_diagnostic(response) for response in responses)

  def _diagnostic_request(self, command: str):
    compact = command.replace(" ", "")
    # ELM accepts an optional expected-response-count nibble after a request.
    if len(compact) % 2 == 1 and compact[-1].isdigit():
      compact = compact[:-1]
    if len(compact) < 4 or len(compact) % 2 or not _HEX_RE.fullmatch(compact):
      return "?"
    request = bytes.fromhex(compact)
    responses = self.vehicle.respond(self.request_header, request)
    if self.receive_header is not None:
      responses = [response for response in responses if response.header == self.receive_header]
    if not responses:
      return "NO DATA"
    return self._line_end.join(self._format_diagnostic(response) for response in responses)

  def execute(self, raw_command: str):
    command = raw_command.strip().upper()
    if not command:
      command = self.last_command
    if not command:
      return ""
    self.last_command = command
    if command.startswith("AT"):
      return self._handle_at(command)
    if command.startswith("ST"):
      return self._handle_st(command)
    return self._diagnostic_request(command)

  def _render(self, command: str, result: str):
    parts = []
    if self.echo and command:
      parts.append(command)
    if result:
      parts.append(result)
    return (self._line_end.join(parts) + self._line_end + ">").encode("ascii")

  def feed(self, data: bytes):
    if not isinstance(data, bytes):
      data = bytes(data)
    try:
      data.decode("ascii")
    except UnicodeDecodeError:
      self._buffer.clear()
      return self._render("", "?")
    self._buffer.extend(data)
    if len(self._buffer) > MAX_COMMAND_BUFFER_BYTES:
      self._buffer.clear()
      return self._render("", "BUFFER FULL")

    output = bytearray()
    while b"\r" in self._buffer:
      raw, _, remainder = self._buffer.partition(b"\r")
      self._buffer = bytearray(remainder.lstrip(b"\n"))
      command = raw.decode("ascii").strip()
      output.extend(self._render(command, self.execute(command)))
    return bytes(output)


class ObdSessionManager:
  """Owns per-central ELM state and prevents cross-device stream corruption."""

  def __init__(self, vehicle: VirtualObdVehicle):
    self.vehicle = vehicle
    self._sessions: dict[str, Elm327Session] = {}
    self._active_client: str | None = None
    self._lock = threading.RLock()

  def feed(self, client: str, data: bytes):
    client = str(client or "unknown")
    with self._lock:
      if self._active_client not in (None, client):
        return b"BUSY\r>"
      self._active_client = client
      session = self._sessions.setdefault(client, Elm327Session(self.vehicle))
      return session.feed(data)

  def disconnect(self, client: str):
    client = str(client or "unknown")
    with self._lock:
      self._sessions.pop(client, None)
      if self._active_client == client:
        self._active_client = None

  def clear(self):
    with self._lock:
      self._sessions.clear()
      self._active_client = None
