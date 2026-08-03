import os
import struct

import usb1

from cereal import custom
from opendbc.car.hyundai.values import CAR, HyundaiFlags
from opendbc.car.structs import CarParams
from panda import FW_PATH, Panda
from openpilot.common.params import Params, UnknownKeyName
from openpilot.common.swaglog import cloudlog


STATUS_VERSION = 4
LEGACY_STATUS_VERSION = 3
H7_APP = "panda_h7.bin.signed"
FIRMWARE_NAMES = (
  "panda_h7_ev9_long_preinit.bin.signed",
  "panda_h7_ev9_long_preinit_hkg_remote.bin.signed",
)
SAFETY_PARAM = 0x8495
OPTIONAL_SAFETY_PARAM = 0x800

STATE_COLLECTING = 0
STATE_WAIT_SESSION = 1
STATE_WAIT_COMM_CONTROL = 2
STATE_WAIT_SUPPRESSION = 3
STATE_ACTIVE = 4
STATE_HANDOFF = 5
STATE_ABORTED = 6
STATE_RESTORING = 7
STATE_READY_PENDING_RESPONSE = 8

FLAG_START_INTENT = 0x02
FLAG_SUPPRESSION_CONFIRMED = 0x04
FLAG_BRIDGE_ACTIVE = 0x08
FLAG_HOST_HANDOFF = 0x10
FLAG_RESTORE_SENT = 0x40

IN_FLIGHT_STATES = frozenset({
  STATE_WAIT_SESSION,
  STATE_WAIT_COMM_CONTROL,
  STATE_WAIT_SUPPRESSION,
  STATE_ACTIVE,
  STATE_HANDOFF,
  STATE_RESTORING,
  STATE_READY_PENDING_RESPONSE,
})
PRESERVE_FLAGS = (FLAG_START_INTENT | FLAG_SUPPRESSION_CONFIRMED | FLAG_BRIDGE_ACTIVE |
                  FLAG_HOST_HANDOFF | FLAG_RESTORE_SENT)

# This reader is intentionally local to the wrapper. It must inspect ownership
# before importing or flashing a newer Panda Python package.
LEGACY_STATUS_STRUCT = struct.Struct("<14BH11I")
STATUS_STRUCT = struct.Struct("<14BH12I")
TIMING_STRUCT = struct.Struct("<4B15I")

def firmware_name(hkg_remote_start: bool) -> str:
  return FIRMWARE_NAMES[1 if hkg_remote_start else 0]


def firmware_path(panda: Panda, hkg_remote_start: bool) -> str:
  if panda.get_mcu_type().config.app_fn != H7_APP:
    return os.path.join(FW_PATH, panda.get_mcu_type().config.app_fn)
  selected_path = os.path.join(FW_PATH, firmware_name(hkg_remote_start))
  if not os.path.isfile(selected_path):
    raise FileNotFoundError(f"Selected EV9 Panda preinit firmware not found: {selected_path}")
  return selected_path


def expected_signature(panda: Panda, hkg_remote_start: bool) -> bytes:
  try:
    return Panda.get_signature_from_firmware(firmware_path(panda, hkg_remote_start))
  except Exception:
    cloudlog.exception("Error computing expected EV9 Panda preinit firmware signature")
    return b""


def enabled_from_params(params: Params) -> bool:
  """Select preinit firmware only for an explicitly armed, persistently identified EV9."""
  try:
    enabled = (params.get_bool("EV9LongPreinitPanda") and
               params.get_bool("OpenpilotEnabledToggle") and
               params.get_bool("AlphaLongitudinalEnabled"))
  except UnknownKeyName:
    return False
  if not enabled:
    return False

  cached_fpcp = params.get("StarPilotCarParamsPersistent")
  if not cached_fpcp:
    return False
  try:
    with custom.StarPilotCarParams.from_bytes(cached_fpcp):
      pass
  except Exception:
    cloudlog.exception("Unable to read persistent StarPilotCarParams for EV9 Panda firmware selection")
    return False

  cached_params = params.get("CarParamsPersistent")
  if cached_params is None:
    return False
  try:
    with CarParams.from_bytes(cached_params) as CP:
      if len(CP.safetyConfigs) == 0:
        return False
      safety = CP.safetyConfigs[-1]
      required_flags = (HyundaiFlags.CANFD | HyundaiFlags.EV | HyundaiFlags.CANFD_LKA_STEERING |
                        HyundaiFlags.CANFD_LKA_STEERING_ALT | HyundaiFlags.CANFD_ANGLE_STEERING)
      return (CP.brand == "hyundai" and CP.carFingerprint == CAR.KIA_EV9 and
              CP.openpilotLongitudinalControl and not CP.pcmCruise and
              safety.safetyModel == CarParams.SafetyModel.hyundaiCanfd and
              (safety.safetyParam & ~OPTIONAL_SAFETY_PARAM) == SAFETY_PARAM and
              (CP.flags & int(required_flags)) == int(required_flags) and
              any(fw.ecu == CarParams.Ecu.adas and fw.address == 0x730 for fw in CP.carFw))
  except Exception:
    cloudlog.exception("Unable to read persistent CarParams for EV9 Panda firmware selection")
    return False


def _raw_status(panda: Panda):
  handle = getattr(panda, "_handle", None)
  if handle is None:
    return None

  try:
    dat = handle.controlRead(Panda.REQUEST_IN, 0xe9, 0, 0, STATUS_STRUCT.size)
  except usb1.USBError:
    return None

  if len(dat) == STATUS_STRUCT.size and dat[0] == STATUS_VERSION:
    status = STATUS_STRUCT.unpack(dat)
  elif len(dat) == LEGACY_STATUS_STRUCT.size and dat[0] == LEGACY_STATUS_VERSION:
    legacy_status = LEGACY_STATUS_STRUCT.unpack(dat)
    status = (*legacy_status, 0)
  else:
    return None

  result = {
    "valid": True,
    "version": status[0],
    "state": status[1],
    "flags": status[13] if status[0] == STATUS_VERSION else 0,
    "fingerprint": status[2],
    "attempts": status[3],
    "last_service": status[4],
    "last_response": status[5],
    "last_nrc": status[6],
    "communication_type": status[7],
    "trigger": status[8],
    "first_ecan_len": status[9],
    "powertrain_state": status[10],
    "powertrain_boot_state": status[11],
    "powertrain_init_state": status[12],
    "first_ecan_addr": status[14],
    "first_can_us": status[15],
    "state_started_us": status[16],
    "trigger_us": status[17],
    "first_ecan_us": status[18],
    "driver_braking_us": status[19],
    "pre_ready_us": status[20],
    "ignition_us": status[21],
    "session_response_us": status[22],
    "comm_control_us": status[23],
    "last_powertrain_us": status[24],
    "ready_us": status[25],
    "outcome_us": status[26],
    "timing_valid": False,
  }

  if result["version"] == STATUS_VERSION:
    try:
      timing_dat = handle.controlRead(Panda.REQUEST_IN, 0xe9, 1, 0, TIMING_STRUCT.size)
    except usb1.USBError:
      return result
    if len(timing_dat) == TIMING_STRUCT.size:
      timing = TIMING_STRUCT.unpack(timing_dat)
      if timing[0] == STATUS_VERSION and timing[1] == 1:
        try:
          verified_dat = handle.controlRead(Panda.REQUEST_IN, 0xe9, 0, 0, STATUS_STRUCT.size)
        except usb1.USBError:
          return result
        if len(verified_dat) == STATUS_STRUCT.size and verified_dat[0] == STATUS_VERSION:
          verified = STATUS_STRUCT.unpack(verified_dat)
          coherent = all(status[index] == verified[index] for index in (0, 1, 13, 17, 22, 23, 25, 26)) and \
            timing[2] == verified[13] and timing[6] == verified[22] and timing[7] == verified[23] and \
            timing[12] == verified[25]
          if coherent:
            result.update({
              "timing_valid": True,
              "timing_flags": timing[2],
              "cycle_started_us": timing[4],
              "session_request_us": timing[5],
              "timing_session_response_us": timing[6],
              "timing_comm_control_us": timing[7],
              "comm_control_response_us": timing[8],
              "last_critical_adas_us": timing[9],
              "first_replacement_us": timing[10],
              "suppression_confirmed_us": timing[11],
              "timing_ready_us": timing[12],
              "handoff_us": timing[13],
              "restore_us": timing[14],
              "abort_us": timing[15],
              "last_host_tx_us": timing[16],
              "last_tester_present_us": timing[17],
              "last_vehicle_frame_us": timing[18],
            })
          else:
            result["valid"] = False
  return result


def get_status(panda: Panda):
  raw_status = _raw_status(panda)
  if raw_status is not None or getattr(panda, "_handle", None) is not None:
    return raw_status

  status_reader = getattr(panda, "get_ev9_long_preinit_status", None)
  if status_reader is not None:
    try:
      status = status_reader()
      if status is not None:
        return status
    except usb1.USBError:
      pass
  return _raw_status(panda)


def status_valid(status) -> bool:
  return status is not None and status.get("valid", True) and status.get("version") in (
    LEGACY_STATUS_VERSION, STATUS_VERSION,
  )


def active(status) -> bool:
  if not status_valid(status):
    return False
  return (status.get("state") in (STATE_ACTIVE, STATE_HANDOFF) or
          bool(status.get("flags", 0) & (FLAG_SUPPRESSION_CONFIRMED | FLAG_BRIDGE_ACTIVE)))


def must_preserve(status) -> bool:
  if not status_valid(status):
    return False
  legacy_ambiguous_disable = status.get("version") == LEGACY_STATUS_VERSION and status.get("comm_control_us", 0) != 0
  return (legacy_ambiguous_disable or status.get("state") in IN_FLIGHT_STATES or
          bool(status.get("flags", 0) & PRESERVE_FLAGS))


def firmware_selected(panda: Panda, enabled: bool) -> bool:
  return enabled and panda.is_internal() and panda.get_mcu_type().config.app_fn == H7_APP


def resident_signature(signature: bytes) -> bool:
  if not signature:
    return False
  for name in FIRMWARE_NAMES:
    path = os.path.join(FW_PATH, name)
    if os.path.isfile(path) and Panda.get_signature_from_firmware(path) == signature:
      return True
  return False


def status_snapshot(status):
  if not status_valid(status):
    return None
  return tuple(status.get(key, 0) for key in (
    "version", "state", "flags", "attempts", "state_started_us", "comm_control_us", "outcome_us",
  ))


def status_stable(first_status, second_status) -> bool:
  return status_snapshot(first_status) == status_snapshot(second_status)


def flash_blocked(status, firmware_selected: bool, ignition_on: bool,
                  resident_firmware: bool = False, status_stable: bool = True) -> bool:
  if resident_firmware and not status_valid(status):
    return True
  sensitive = firmware_selected or resident_firmware or status_valid(status)
  return must_preserve(status) or (sensitive and (ignition_on or not status_stable))


def reset_blocked(status, firmware_selected: bool, resident_firmware: bool = False,
                  ignition_on: bool = False, status_stable: bool = True) -> bool:
  return firmware_selected or resident_firmware or must_preserve(status) or \
    (status_valid(status) and (ignition_on or not status_stable))


def ignition_on(panda: Panda) -> bool:
  health = panda.health()
  return bool(health["ignition_line"] or health["ignition_can"])
