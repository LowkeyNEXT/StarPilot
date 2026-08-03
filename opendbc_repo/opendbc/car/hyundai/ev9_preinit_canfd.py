import numpy as np

from opendbc.car import CanData
from opendbc.car.common.conversions import Conversions as CV
from opendbc.car.hyundai.hyundaicanfd import _create_angle_adas_cmd_msg, _set_ccnc_message_signals, hkg_can_fd_checksum


EV9_ACCEL_BRAKE_ALT_TEMPLATE = bytes.fromhex("00000000ff006f00e80400001201030055ffff0000000000")
EV9_ADRV_TEMPLATES = {
  0x160: bytes.fromhex("0000000100000000fffc0100a8001000"),
  0x1DA: bytes.fromhex("0000002200110000000000000000000000000000000000000000000000000000"),
  0x1EA: bytes.fromhex("000000080000000000000000000000ff000000000000000000000000000f0f00"),
  0x200: bytes.fromhex("00000014801a0000"),
  0x345: bytes.fromhex("0000001500560000"),
  0x161: bytes.fromhex("0000000000000000c0fff0c003000000000000000000000000ff000000000000"),
  0x162: bytes.fromhex("0000002700000000c0ff00000000000000000000000000000000000000000000"),
  0x1BA: bytes.fromhex("00000000000000880200000000000000000000000000000f"),
  0x1E5: bytes.fromhex("00000000000000000000220300000080"),
  0x1E0: bytes.fromhex("00000002000000000000000000000000"),
  0x38C: bytes.fromhex("000000f71f000000000000000000000000000000000000000000000000000000"),
}
EV9_ADRV_PERIODS = (
  (0x160, 2),
  (0x1EA, 5),
  (0x200, 5),
  (0x345, 20),
  (0x1DA, 100),
  (0x1E0, 5),
  (0x38C, 20),
)

# OFF-to-READY continuity captured immediately before ADAS suppression. These
# baselines are process-local and reset before every pre-fingerprint attempt.
_LIVE_TEMPLATES: dict[int, bytes] = {}
_COUNTER_BASES: dict[int, int] = {}
_SCC_CONTROL_LIVE_TEMPLATE: bytes | None = None
_SCC_CONTROL_COUNTER_BASE = 0


def scc_control_baseline_available() -> bool:
  return _SCC_CONTROL_LIVE_TEMPLATE is not None


def set_adrv_baselines(messages: list[CanData]) -> None:
  global _SCC_CONTROL_LIVE_TEMPLATE, _SCC_CONTROL_COUNTER_BASE
  _LIVE_TEMPLATES.clear()
  _COUNTER_BASES.clear()
  _SCC_CONTROL_LIVE_TEMPLATE = None
  _SCC_CONTROL_COUNTER_BASE = 0
  for msg in messages:
    dat = bytes(msg.dat)
    if msg.src == 0 and msg.address == 0x100 and len(dat) == 24:
      _COUNTER_BASES[msg.address] = dat[2]
    elif msg.src == 1 and msg.address in (0x12A, 0xCB) and len(dat) in (16, 24):
      _COUNTER_BASES[msg.address] = dat[2]
      _LIVE_TEMPLATES[msg.address] = dat
    elif msg.src == 1 and msg.address in EV9_ADRV_TEMPLATES and len(dat) == len(EV9_ADRV_TEMPLATES[msg.address]):
      _COUNTER_BASES[msg.address] = dat[2]
      if msg.address != 0x160:
        _LIVE_TEMPLATES[msg.address] = dat
    elif msg.src == 1 and msg.address == 0x1A0 and len(dat) == 32:
      _SCC_CONTROL_LIVE_TEMPLATE = dat
      _SCC_CONTROL_COUNTER_BASE = dat[2]


def _finalize_message(address: int, dat: bytearray, bus: int) -> CanData:
  crc = hkg_can_fd_checksum(address, None, dat)
  dat[0] = crc & 0xFF
  dat[1] = (crc >> 8) & 0xFF
  return CanData(address, bytes(dat), bus)


def _message(address: int, bus: int, counter: int) -> CanData:
  template = _LIVE_TEMPLATES.get(address, EV9_ADRV_TEMPLATES.get(address))
  if template is None:
    raise KeyError(address)
  dat = bytearray(template)
  if address in _COUNTER_BASES:
    counter += _COUNTER_BASES[address] + 1
  dat[2] = counter & 0xFF
  return _finalize_message(address, dat, bus)


def create_adrv_message(address: int, bus: int, counter: int) -> CanData:
  """Recreate a captured EV9 ADAS support payload with fresh integrity fields."""
  return _message(address, bus, counter)


def create_periodic_adrv_messages(bus: int, frame: int) -> list[CanData]:
  return [
    _message(address, bus, frame // period)
    for address, period in EV9_ADRV_PERIODS if frame % period == 0
  ]


def _message_with_signals(packer, CAN, address: int, counter: int,
                          message_name: str, values: dict) -> CanData:
  msg = _message(address, CAN.ECAN, counter)
  dat = bytearray(msg.dat)
  _set_ccnc_message_signals(packer, message_name, dat, values)
  return _finalize_message(address, dat, CAN.ECAN)


def create_accelerator_brake_alt(bus: int, counter: int, brake_pressed: bool,
                                 accelerator_pressed: bool) -> CanData:
  dat = bytearray(EV9_ACCEL_BRAKE_ALT_TEMPLATE)
  if 0x100 in _COUNTER_BASES:
    counter += _COUNTER_BASES[0x100] + 1
  dat[2] = counter & 0xFF
  dat[4] = (dat[4] & ~0x01) | int(brake_pressed)
  dat[22] = (dat[22] & ~0x01) | int(accelerator_pressed)
  return _finalize_message(0x100, dat, bus)


def create_direct_angle_command(packer, CAN, apply_angle: float, lat_active: bool,
                                torque_reduction_gain: float, counter: int = 0) -> CanData:
  if 0xCB not in _LIVE_TEMPLATES:
    return _create_angle_adas_cmd_msg(packer, CAN, apply_angle, lat_active, torque_reduction_gain)
  values = {
    "ADAS_ActvACISta": 0,
    "ADAS_ActvACILvl2Sta": 2 if lat_active else 1,
    "ADAS_StrAnglReqVal": apply_angle,
    "ADAS_ACIAnglTqRedcGainVal": torque_reduction_gain if lat_active else 0.0,
    "FCA_ESA_ActvSta": 0,
    "FCA_ESA_TqBstGainVal": 0.0,
  }
  return _message_with_signals(packer, CAN, 0xCB, counter, "ADAS_CMD_35_10ms", values)


def create_inactive_steering_messages(packer, CAN, steering_angle: float, counter: int = 0) -> list[CanData]:
  lfa_values = {
    "LKA_MODE": 2, "LKA_ICON": 1, "TORQUE_REQUEST": 0, "LKA_ASSIST": 0,
    "STEER_REQ": 0, "STEER_MODE": 0, "HAS_LANE_SAFETY": 0,
    "NEW_SIGNAL_1": 0, "NEW_SIGNAL_2": 0, "DAMP_FACTOR": 100,
  }
  adas_cmd_values = {
    "ADAS_ActvACISta": 0, "ADAS_ActvACILvl2Sta": 1,
    "ADAS_StrAnglReqVal": steering_angle, "ADAS_ACIAnglTqRedcGainVal": 0.0,
    "FCA_ESA_ActvSta": 0, "FCA_ESA_TqBstGainVal": 0.0,
  }
  return [
    _message_with_signals(packer, CAN, 0x12A, counter, "LFA", lfa_values)
    if 0x12A in _LIVE_TEMPLATES else packer.make_can_msg("LFA", CAN.ECAN, lfa_values),
    _message_with_signals(packer, CAN, 0xCB, counter, "ADAS_CMD_35_10ms", adas_cmd_values)
    if 0xCB in _LIVE_TEMPLATES else _create_angle_adas_cmd_msg(packer, CAN, steering_angle, False, 0.0),
  ]


def create_neutral_blindspot_messages(packer, CAN, counter: int, drive_gear: bool) -> list[CanData]:
  values = {
    "BCW_IndSta": 1,
    "BCA_OnOffEquip2Sta": 2,
    "BCA_Sta": int(drive_gear),
    "BCW_LtIndSta": 0,
    "BCW_RtIndSta": 0,
    "BCW_LtSndWrngSta": 0,
    "BCW_RtSndWrngSta": 0,
    "OSMrrLamp_LtIndSta": 0,
    "OSMrrLamp_RtIndSta": 0,
  }
  rear_message = _message_with_signals(packer, CAN, 0x1BA, counter, "BLINDSPOTS_REAR_CORNERS", values)
  dat = bytearray(rear_message.dat)
  dat[17] = 0x01
  dat[21] = 0x00
  dat[22] = 0x00
  return [_finalize_message(0x1BA, dat, CAN.ECAN), _message(0x1E5, CAN.ECAN, counter)]


def create_acc_control(packer, CAN, counter: int, enabled: bool, accel_raw: float, accel_value: float,
                       stop_request: bool, cruise_standstill: bool, gas_override: bool, set_speed: float,
                       main_mode_acc: int, lead_distance: float, lead_rel_speed: float, lead_visible: bool,
                       v_ego: float, jerk_lower: float = 0.7, jerk_upper: float = 0.7) -> CanData:
  if not enabled or gas_override or stop_request:
    accel_raw = 0.0
    accel_value = 0.0
  lead_visible = bool(lead_visible)
  desired_headway = min(max(round(1.625 * max(v_ego, 0.0), 1), 3.5), 204.6) if enabled else 204.6
  values = {
    "ACCMode": 0 if not enabled else (2 if gas_override else 1),
    "MainMode_ACC": int(bool(main_mode_acc)), "StopReq": int(stop_request and enabled),
    "CRUISE_STANDSTILL": int(cruise_standstill and stop_request and enabled),
    "aReqValue": accel_value, "aReqRaw": accel_raw, "VSetDis": set_speed,
    "JerkLowerLimit": jerk_lower if enabled else 1.0, "JerkUpperLimit": jerk_upper if enabled else 3.0,
    "ACC_ObjDist": float(np.clip(lead_distance, 0.0, 204.7)) if lead_visible else 204.6,
    "ACC_ObjRelSpd": float(np.clip(lead_rel_speed, -16.4, 34.7)) if lead_visible else 34.6,
    "ObjValid": int(not lead_visible), "OBJ_STATUS": 2 if enabled and lead_visible else 0,
    "NEW_SIGNAL_3": 2 if lead_visible else 0, "NEW_SIGNAL_15": desired_headway,
    "SET_ME_2": 4, "SET_ME_3": 3, "SET_ME_TMP_64": 0x64,
    "DISTANCE_SETTING": 7 if enabled else 0,
  }
  if _SCC_CONTROL_LIVE_TEMPLATE is None:
    return packer.make_can_msg("SCC_CONTROL", CAN.ECAN, values)
  dat = bytearray(_SCC_CONTROL_LIVE_TEMPLATE)
  _set_ccnc_message_signals(packer, "SCC_CONTROL", dat, values)
  dat[2] = (_SCC_CONTROL_COUNTER_BASE + counter + 1) & 0xFF
  return _finalize_message(0x1A0, dat, CAN.ECAN)


def create_basic_status_messages(packer, CAN, counter: int, enabled: bool,
                                 main_cruise_enabled: bool, hud, out, is_metric: bool,
                                 steering_available: bool, steering_active: bool,
                                 hba_icon: int) -> list[CanData]:
  cruise_speed = round(out.vCruiseCluster * (1 if is_metric else CV.KPH_TO_MPH))
  display_speed = (40 if is_metric else 25) if cruise_speed > (145 if is_metric else 90) else max(cruise_speed, 0)
  main_standby = bool(main_cruise_enabled and not enabled)
  values_161 = {
    "FCA_ICON": 1, "FCA_ALT_ICON": 0, "LKA_ICON": 0, "FCA_IMAGE": 0,
    "ALERTS_1": 0, "ALERTS_2": 0, "ALERTS_3": 0, "ALERTS_4": 0, "ALERTS_5": 0,
    "SOUNDS_1": 0, "SOUNDS_2": 0, "SOUNDS_3": 0, "SOUNDS_4": 0,
    "LFA_ICON": (2 if steering_active else 1) if steering_available else 0,
    "HBA_ICON": hba_icon if hba_icon in (1, 2) else 0,
    "HDA_ICON": 2 if enabled else 1 if main_standby else 0,
    "CENTERLINE": 0, "TARGET": 0, "TARGET_DISTANCE": 204.6,
    "LANELINE_LEFT": 0, "LANELINE_LEFT_POSITION": 15,
    "LANELINE_RIGHT": 0, "LANELINE_RIGHT_POSITION": 15,
    "LANELINE_CURVATURE": 15, "LANE_ZOOM": 1,
    "LCA_LEFT_ICON": 1 if enabled or main_standby else 0,
    "LCA_RIGHT_ICON": 1 if enabled or main_standby else 0,
    "SETSPEED": 3 if enabled else 1 if main_standby else 0,
    "SETSPEED_HUD": 2 if enabled else 1 if main_standby else 0,
    "SETSPEED_SPEED": display_speed if enabled or main_standby else 255,
    "DISTANCE": hud.leadDistanceBars if enabled else 0,
    "DISTANCE_SPACING": 3 if enabled or main_standby else 0,
    "DISTANCE_LEAD": 0, "DISTANCE_CAR": 2 if enabled else 1 if main_standby else 0,
    "BCA_LEFT": 0, "BCA_RIGHT": 0, "LCA_LEFT_ARROW": 0, "LCA_RIGHT_ARROW": 0,
  }
  values_162 = {fault: 0 for fault in (
    "FAULT_FSS", "FAULT_FCA", "FAULT_LSS", "FAULT_SLA", "FAULT_HDA", "FAULT_DAS", "FAULT_LFA", "FAULT_DAW",
    "FAULT_HBA", "FAULT_ESS",
  )}
  values_162.update({
    "COUNTRY": 7, "SPEEDLIMIT": 0, "SPEEDLIMIT_FLASH": 2,
    "SIGNS": 0, "SPEEDLIMIT_WEATHER": 0, "VIBRATE": 0,
    "LEAD": 0, "LEAD_DISTANCE": 0.0, "LEAD_LATERAL": 0.0,
    "LEAD_ALT": 0, "LEAD_ALT_DISTANCE": 0.0, "LEAD_ALT_LATERAL": 0.0,
    "LEAD_LEFT": 0, "LEAD_LEFT_DISTANCE": 0.0, "LEAD_LEFT_LATERAL": 0.0,
    "LEAD_RIGHT": 0, "LEAD_RIGHT_DISTANCE": 0.0, "LEAD_RIGHT_LATERAL": 0.0,
    "LEAD_LEFT_REAR_STATUS": 0, "LEAD_LEFT_REAR_DISTANCE": 0.0, "LEAD_LEFT_REAR_LATERAL": 0.0,
    "LEAD_RIGHT_REAR_STATUS": 0, "LEAD_RIGHT_REAR_DISTANCE": 0.0, "LEAD_RIGHT_REAR_LATERAL": 0.0,
  })
  return [
    _message_with_signals(packer, CAN, 0x161, counter, "CCNC_0x161", values_161),
    _message_with_signals(packer, CAN, 0x162, counter, "CCNC_0x162", values_162),
  ]
