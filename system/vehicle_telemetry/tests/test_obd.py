from openpilot.system.vehicle_telemetry.obd import (
  ELM_IDENTITY,
  EV_REQUEST_HEADER,
  EV_RESPONSE_HEADER,
  HKMC_BMS_PAGE_101,
  HKMC_BMS_PAGE_105,
  STARPILOT_CACHED_TELEMETRY_MAX_AGE_SECONDS,
  STARPILOT_DID_COLLECTED_AT,
  STARPILOT_DID_DISTANCE_TO_EMPTY,
  STARPILOT_DID_SOC,
  STARPILOT_DID_STATUS,
  STARPILOT_REQUEST_HEADER,
  Elm327Session,
  ObdSessionManager,
  VirtualObdVehicle,
)


NOW = 2_000.0


def telemetry_snapshot(**overrides):
  snapshot = {
    "schemaVersion": 1,
    "updatedAt": NOW - 1.0,
    "stateOfChargePercent": 77.5,
    "distanceToEmptyKilometers": 408.0,
    "isCharging": False,
    "isPluggedIn": True,
    "batteryCurrentAmps": -12.3,
    "batteryVoltageVolts": 627.1,
    "minimumBatteryTemperatureCelsius": 26.0,
    "maximumBatteryTemperatureCelsius": 28.0,
    "remainingEnergyKilowattHours": 97.208,
    "speedMetersPerSecond": 12.5,
    "standstill": False,
  }
  snapshot.update(overrides)
  return snapshot


def session(snapshot=None):
  value = telemetry_snapshot() if snapshot is None else snapshot
  return Elm327Session(VirtualObdVehicle(lambda: value, clock=lambda: NOW))


def test_reset_and_transport_initialization_commands():
  elm = session()
  assert elm.feed(b"ATZ\r") == f"ATZ\r\n{ELM_IDENTITY}\r\n>".encode()
  assert elm.feed(b"ATE0\r") == b"OK\r\n>"
  assert elm.feed(b"ATSP 00\rATH 0\rATS 0\r") == b"OK\r\n>OK\r\n>OK\r\n>"
  assert elm.feed(b"ATI\r") == f"{ELM_IDENTITY}\r\n>".encode()
  assert elm.feed(b"ATFE\r") == b"OK\r\n>"


def test_fragmented_command_and_repeat_last_command():
  elm = session()
  assert elm.feed(b"AT") == b""
  assert elm.feed(b"I\r") == f"ATI\r\n{ELM_IDENTITY}\r\n>".encode()
  assert elm.feed(b"\r") == f"{ELM_IDENTITY}\r\n>".encode()


def test_standard_soc_and_speed_are_synthesized_from_live_cache():
  elm = session()
  elm.feed(b"ATE0\rATL0\rATS0\r")
  assert elm.feed(b"010D\r") == b"410D2D\r>"
  assert elm.feed(b"ATSH7E4\r") == b"OK\r>"
  assert elm.request_header == EV_REQUEST_HEADER
  assert elm.feed(b"015B\r") == b"415BC6\r>"


def test_supported_pid_ranges_chain_to_traction_soc():
  elm = session()
  elm.feed(b"ATE0\rATL0\rATS0\r")
  assert elm.feed(b"0100\r") == b"410000080011\r>"
  assert elm.feed(b"0120\r") == b"412000000001\r>"
  assert elm.feed(b"0140\r") == b"414000000020\r>"
  assert elm.feed(b"0160\r") == b"NO DATA\r>"


def test_safe_stpx_read_uses_virtual_vehicle_and_never_raw_can():
  elm = session()
  elm.feed(b"ATE0\rATL0\rATS0\r")
  assert elm.feed(b"STI\r") == b"STN2230 v5.6.19\r>"
  assert elm.feed(b"STPXH:7E4,D:015B,R:1,T:32\r") == b"415BC6\r>"
  assert elm.feed(b"STPXH:7E4,D:2E010100,R:1\r") == b"NO DATA\r>"
  assert elm.feed(b"STPXH:7E4,L:4,R:1\r") == b"?\r>"


def test_stale_or_invalid_cache_never_becomes_obd_telemetry():
  stale = telemetry_snapshot(updatedAt=NOW - 30.0)
  elm = session(stale)
  elm.feed(b"ATE0\rATL0\r")
  assert elm.feed(b"015B\r") == b"NO DATA\r>"

  elm.feed(f"ATSH{STARPILOT_REQUEST_HEADER:03X}\r".encode())
  assert elm.feed(f"22{STARPILOT_DID_SOC:04X}\r".encode()) == b"62 D1 01 03 07\r>"
  assert elm.feed(f"22{STARPILOT_DID_COLLECTED_AT:04X}\r".encode()) == b"62 D1 05 00 00 07 B2\r>"

  responses = elm.vehicle.respond(EV_REQUEST_HEADER, bytes.fromhex("220105"))
  assert responses == []

  invalid = telemetry_snapshot(stateOfChargePercent=101.0)
  elm = session(invalid)
  elm.feed(b"ATE0\rATL0\r")
  assert elm.feed(b"015B\r") == b"NO DATA\r>"


def test_expired_cache_never_becomes_starpilot_telemetry():
  stale = telemetry_snapshot(updatedAt=NOW - STARPILOT_CACHED_TELEMETRY_MAX_AGE_SECONDS - 1)
  elm = session(stale)
  elm.feed(b"ATE0\rATL0\rATS0\r")
  elm.feed(f"ATSH{STARPILOT_REQUEST_HEADER:03X}\r".encode())
  assert elm.feed(f"22{STARPILOT_DID_STATUS:04X}\r".encode()) == b"NO DATA\r>"


def test_documented_starpilot_dids_use_the_same_obd_channel():
  elm = session()
  elm.feed(b"ATE0\rATL0\rATH1\rATS1\r")
  elm.feed(f"ATSH{STARPILOT_REQUEST_HEADER:03X}\r".encode())

  distance = elm.feed(f"22{STARPILOT_DID_DISTANCE_TO_EMPTY:04X}\r".encode()).decode()
  assert distance == "7EE 05 62 D1 02 0F F0\r>"

  status = elm.feed(f"22{STARPILOT_DID_STATUS:04X}\r".encode()).decode()
  assert status == "7EE 07 62 D1 00 01 02 00 01\r>"


def test_abrp_hkmc_pages_translate_only_live_validated_soc():
  vehicle = VirtualObdVehicle(telemetry_snapshot, clock=lambda: NOW)

  page101 = vehicle.respond(0x7DF, bytes.fromhex("220101"))
  assert len(page101) == 1
  assert page101[0].header == EV_RESPONSE_HEADER
  assert page101[0].payload[:3] == bytes.fromhex("620101")
  assert page101[0].payload[3 + 4] == 155
  assert page101[0].payload[3 + 9] == 0x20
  assert page101[0].payload[3 + 10:3 + 12] == bytes.fromhex("FF85")
  assert page101[0].payload[3 + 12:3 + 14] == bytes.fromhex("187F")
  assert page101[0].payload[3 + 14:3 + 20] == bytes([28, 26, 28, 26, 28, 26])

  page105 = vehicle.respond(EV_REQUEST_HEADER, bytes.fromhex("220105"))
  assert len(page105) == 1
  assert page105[0].header == EV_RESPONSE_HEADER
  assert page105[0].payload[:3] == bytes.fromhex("620105")
  assert page105[0].payload[3 + 28:3 + 30] == bytes.fromhex("BDDC")
  assert page105[0].payload[3 + 31] == 155

  unknown = vehicle.respond(EV_REQUEST_HEADER, bytes.fromhex("220100"))
  assert unknown == []


def test_abrp_hkmc_page_preserves_known_charging_state():
  charging = telemetry_snapshot(isCharging=True, isPluggedIn=True)
  vehicle = VirtualObdVehicle(lambda: charging, clock=lambda: NOW)
  response = vehicle.respond(EV_REQUEST_HEADER, bytes.fromhex(f"22{HKMC_BMS_PAGE_101:04X}"))[0]
  assert response.payload[3 + 9] == 0xA0

  page105 = vehicle.respond(EV_REQUEST_HEADER, bytes.fromhex(f"22{HKMC_BMS_PAGE_105:04X}"))[0]
  assert page105.payload[3 + 31] == 155


def test_unknown_and_write_capable_commands_are_rejected_locally():
  elm = session()
  elm.feed(b"ATE0\rATL0\r")
  for command in (b"04\r", b"1003\r", b"2E010100\r", b"3101FFFF\r", b"STPX D:DEADBEEF\r"):
    assert elm.feed(command) in (b"NO DATA\r>", b"?\r>")


def test_receive_header_filter_and_iso_tp_formatting():
  elm = session()
  elm.feed(b"ATE0\rATL0\rATH1\rATS0\r")
  elm.feed(f"ATSH{STARPILOT_REQUEST_HEADER:03X}\rATCRA7ED\r".encode())
  assert elm.feed(f"22{STARPILOT_DID_STATUS:04X}\r".encode()) == b"NO DATA\r>"
  elm.feed(b"ATCRA7EE\r")
  assert elm.feed(f"22{STARPILOT_DID_STATUS:04X}\r".encode()).startswith(b"7EE07")


def test_session_manager_prevents_cross_device_stream_corruption():
  vehicle = VirtualObdVehicle(telemetry_snapshot, clock=lambda: NOW)
  manager = ObdSessionManager(vehicle)
  assert ELM_IDENTITY.encode() in manager.feed("phone-a", b"ATI\r")
  assert manager.feed("phone-b", b"ATI\r") == b"BUSY\r>"
  manager.disconnect("phone-a")
  assert ELM_IDENTITY.encode() in manager.feed("phone-b", b"ATI\r")


def test_non_ascii_and_oversized_input_fail_closed():
  elm = session()
  assert elm.feed(b"\xff\r").endswith(b"?\r\n>")
  assert b"BUFFER FULL" in elm.feed(b"A" * 513)
