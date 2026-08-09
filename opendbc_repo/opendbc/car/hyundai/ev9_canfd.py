import numpy as np

from opendbc.car import CanData
from opendbc.car.common.conversions import Conversions as CV
from opendbc.car.hyundai.hyundaicanfd import _set_ccnc_message_signals, hkg_can_fd_checksum


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

_LIVE_TEMPLATES: dict[int, bytes] = {}
_COUNTER_BASES: dict[int, int] = {}


def set_adrv_baselines(messages: list[CanData]) -> None:
  """Optionally seed dashboard continuity from live ADAS frames."""
  _LIVE_TEMPLATES.clear()
  _COUNTER_BASES.clear()
  for msg in messages:
    dat = bytes(msg.dat)
    if msg.src == 0 and msg.address == 0x100 and len(dat) == 24:
      _COUNTER_BASES[msg.address] = dat[2]
    elif msg.src == 1 and msg.address in EV9_ADRV_TEMPLATES and len(dat) == len(EV9_ADRV_TEMPLATES[msg.address]):
      _COUNTER_BASES[msg.address] = dat[2]
      if msg.address != 0x160:
        _LIVE_TEMPLATES[msg.address] = dat

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
  dat[4] = (dat[4] & ~0x01) | (0x01 if brake_pressed else 0x00)
  dat[22] = (dat[22] & ~0x01) | (0x01 if accelerator_pressed else 0x00)
  return _finalize_message(0x100, dat, bus)


def create_blindspot_status_messages(packer, CAN, counter, left_blindspot=False, right_blindspot=False,
                                     left_escalated=False, right_escalated=False, drive_gear=False,
                                     left_warning_lamp=False, right_warning_lamp=False,
                                     left_sound_active=False, right_sound_active=False):
  left_state = 2 if left_blindspot and left_escalated else (1 if left_blindspot else 0)
  right_state = 2 if right_blindspot and right_escalated else (1 if right_blindspot else 0)
  left_osm_state = 2 if left_warning_lamp else 1 if left_state == 1 else 0
  right_osm_state = 2 if right_warning_lamp else 1 if right_state == 1 else 0
  values = {
    "BCW_IndSta": 1, "BCA_OnOffEquip2Sta": 2, "BCA_Sta": int(drive_gear),
    "BCW_LtIndSta": left_state, "BCW_RtIndSta": right_state,
    "BCW_LtSndWrngSta": int(left_sound_active), "BCW_RtSndWrngSta": int(right_sound_active),
    "OSMrrLamp_LtIndSta": left_osm_state, "OSMrrLamp_RtIndSta": right_osm_state,
  }
  rear_message = _message_with_signals(packer, CAN, 0x1BA, counter, "BLINDSPOTS_REAR_CORNERS", values)
  dat = bytearray(rear_message.dat)
  dat[17] = 0x01 | (left_osm_state << 2) | (right_osm_state << 5)
  dat[21] = 0x60 if left_sound_active or right_sound_active else 0x00
  dat[22] = 0x08 if left_sound_active or right_sound_active else 0x00
  return [_finalize_message(0x1BA, dat, CAN.ECAN), _message(0x1E5, CAN.ECAN, counter)]


def create_adrv_160(bus: int, counter: int) -> CanData:
  return _message(0x160, bus, counter)


def lane_curvature_from_steering_angle(steering_angle_deg: float) -> int:
  curvature_index = max(-15, min(int(steering_angle_deg / 4.5), 15))
  if curvature_index >= 0:
    return 15 + curvature_index
  return 31 if curvature_index == -1 else 13 - abs(curvature_index + 15)


def create_angle_long_status_messages(packer, CP, CAN, counter: int, enabled: bool = False,
                                      main_cruise_enabled: bool = False, hud=None, out=None,
                                      is_metric: bool = True, steering_available: bool = False,
                                      steering_active: bool = False, hba_icon: int = 0,
                                      dash_scene=None) -> list[CanData]:
  cruise_speed = round(out.vCruiseCluster * (1 if is_metric else CV.KPH_TO_MPH)) if out is not None else 0
  display_speed = (40 if is_metric else 25) if cruise_speed > (145 if is_metric else 90) else max(cruise_speed, 0)
  main_standby = bool(main_cruise_enabled and not enabled)
  display_active = bool(enabled or main_standby)
  objects = getattr(dash_scene, "objects", None)
  primary = getattr(objects, "primary", None)
  left = getattr(objects, "left", None)
  right = getattr(objects, "right", None)
  left_rear = getattr(objects, "left_rear", None)
  right_rear = getattr(objects, "right_rear", None)
  objects_active = bool(display_active and objects is not None and getattr(dash_scene, "objects_enabled", True))
  side_objects_active = bool(objects_active and getattr(dash_scene, "side_objects_enabled", True))
  primary_object_state = (2 if enabled else 1) if objects_active and primary is not None else 0
  target_line_distance = getattr(dash_scene, "target_line_distance", None)
  speed_limit_raw = int(getattr(dash_scene, "speed_limit_raw", 0))
  speed_limit_raw = speed_limit_raw if 1 <= speed_limit_raw <= 253 else 0
  speed_limit_warning = bool(getattr(dash_scene, "speed_limit_warning", False))
  lane_outline = getattr(dash_scene, "lane_outline", None)
  lane_change_direction = getattr(dash_scene, "lane_change_direction", None)
  lane_change_active = bool(display_active and lane_change_direction in ("left", "right"))
  headway_enabled = bool(display_active and getattr(dash_scene, "headway_enabled", True))
  desired_curvature = float(getattr(lane_outline, "desired_curvature", 0.0))
  lane_geometry_valid = bool(np.isfinite(desired_curvature) and np.isfinite(CP.wheelbase) and np.isfinite(CP.steerRatio))
  left_lane_visible = bool(display_active and lane_geometry_valid and getattr(lane_outline, "left_visible", False))
  right_lane_visible = bool(display_active and lane_geometry_valid and getattr(lane_outline, "right_visible", False))
  lane_curvature = 15
  if left_lane_visible or right_lane_visible:
    steering_angle_deg = -np.degrees(np.arctan(desired_curvature * CP.wheelbase)) * CP.steerRatio
    lane_curvature = lane_curvature_from_steering_angle(steering_angle_deg)
  if not headway_enabled:
    target_distance = 204.6
  elif target_line_distance is not None:
    target_distance = float(np.clip(target_line_distance, 0.1, 204.7))
  else:
    target_distance = float(np.clip(1.626 * max(float(getattr(out, "vEgo", 0.0)), 0.0), 0.0, 204.7))

  def object_distance(obj) -> float:
    return float(np.clip(float(obj.distance) - 0.2, 0.1, 204.7))

  def rear_object_distance(obj) -> float:
    return float(np.clip(float(obj.distance) - 0.2, 0.1, 25.5))

  values_161 = {
    "FCA_ICON": 1, "FCA_ALT_ICON": 0, "LKA_ICON": 0, "FCA_IMAGE": 0,
    "ALERTS_1": 0, "ALERTS_2": 0, "ALERTS_3": 0, "ALERTS_4": 0, "ALERTS_5": 0,
    "SOUNDS_1": 0, "SOUNDS_2": 0, "SOUNDS_3": 0, "SOUNDS_4": 0,
    "LFA_ICON": (2 if steering_active else 1) if steering_available else 0,
    "HBA_ICON": hba_icon if hba_icon in (1, 2) else 0,
    "HDA_ICON": 2 if enabled else 1 if main_standby else 0,
    "CENTERLINE": 0, "TARGET": 3 if headway_enabled else 0, "TARGET_DISTANCE": target_distance,
    "LANELINE_LEFT": 4 if left_lane_visible and bool(getattr(hud, "leftLaneDepart", False)) else
      6 if left_lane_visible and lane_change_active else 2 if left_lane_visible else 0,
    "LANELINE_LEFT_POSITION": 15,
    "LANELINE_RIGHT": 4 if right_lane_visible and bool(getattr(hud, "rightLaneDepart", False)) else
      6 if right_lane_visible and lane_change_active else 2 if right_lane_visible else 0,
    "LANELINE_RIGHT_POSITION": 15, "LANELINE_CURVATURE": lane_curvature, "LANE_ZOOM": 1,
    "LCA_LEFT_ICON": 2 if lane_change_active else 1 if enabled or main_standby else 0,
    "LCA_RIGHT_ICON": 2 if lane_change_active else 1 if enabled or main_standby else 0,
    "SETSPEED": 3 if enabled else 1 if main_standby else 0,
    "SETSPEED_HUD": 2 if enabled else 1 if main_standby else 0,
    "SETSPEED_SPEED": display_speed if enabled or main_standby else 255,
    "DISTANCE": hud.leadDistanceBars if enabled and hud is not None else 0,
    "DISTANCE_SPACING": 3 if enabled or main_standby else 0,
    "DISTANCE_LEAD": primary_object_state, "DISTANCE_CAR": 2 if enabled else 1 if main_standby else 0,
    "BCA_LEFT": 0, "BCA_RIGHT": 0,
    "LCA_LEFT_ARROW": 2 if lane_change_direction == "left" and lane_change_active else 0,
    "LCA_RIGHT_ARROW": 2 if lane_change_direction == "right" and lane_change_active else 0,
  }
  values_162 = {fault: 0 for fault in (
    "FAULT_FSS", "FAULT_FCA", "FAULT_LSS", "FAULT_SLA", "FAULT_HDA", "FAULT_DAS", "FAULT_LFA", "FAULT_DAW",
    "FAULT_HBA", "FAULT_ESS",
  )}
  values_162.update({
    "COUNTRY": 7, "SPEEDLIMIT": speed_limit_raw,
    "SPEEDLIMIT_FLASH": 4 if speed_limit_warning and speed_limit_raw else 2,
    "SIGNS": 0, "SPEEDLIMIT_WEATHER": 0, "VIBRATE": 0,
    "LEAD": primary_object_state,
    "LEAD_DISTANCE": object_distance(primary) if objects_active and primary is not None else 0.0,
    "LEAD_LATERAL": 0.0, "LEAD_ALT": 0, "LEAD_ALT_DISTANCE": 0.0, "LEAD_ALT_LATERAL": 0.0,
    "LEAD_LEFT": 1 if side_objects_active and left is not None else 0,
    "LEAD_LEFT_DISTANCE": object_distance(left) if side_objects_active and left is not None else 0.0,
    "LEAD_LEFT_LATERAL": 3.0 if side_objects_active and left is not None else 0.0,
    "LEAD_RIGHT": 1 if side_objects_active and right is not None else 0,
    "LEAD_RIGHT_DISTANCE": object_distance(right) if side_objects_active and right is not None else 0.0,
    "LEAD_RIGHT_LATERAL": 3.0 if side_objects_active and right is not None else 0.0,
    "LEAD_LEFT_REAR_STATUS": 1 if side_objects_active and left_rear is not None else 0,
    "LEAD_LEFT_REAR_DISTANCE": rear_object_distance(left_rear) if side_objects_active and left_rear is not None else 0.0,
    "LEAD_LEFT_REAR_LATERAL": 3.0 if side_objects_active and left_rear is not None else 0.0,
    "LEAD_RIGHT_REAR_STATUS": 1 if side_objects_active and right_rear is not None else 0,
    "LEAD_RIGHT_REAR_DISTANCE": rear_object_distance(right_rear) if side_objects_active and right_rear is not None else 0.0,
    "LEAD_RIGHT_REAR_LATERAL": 3.0 if side_objects_active and right_rear is not None else 0.0,
  })
  return [
    _message_with_signals(packer, CAN, 0x161, counter, "CCNC_0x161", values_161),
    _message_with_signals(packer, CAN, 0x162, counter, "CCNC_0x162", values_162),
  ]
