from dataclasses import dataclass

from opendbc.car import CanData, structs
from opendbc.car.hyundai import ev9_canfd
from opendbc.car.hyundai.values import CAR, HyundaiFlags

EV9_STOCK_STATUS_STALE_NS = 200_000_000


def ev9_dynamic_steering_icons(CP, lat_active: bool, steering_active: bool,
                               legacy_lka_icon: int, legacy_lfa_icon: int) -> tuple[int, int, bool | None]:
  applicable = CP.carFingerprint == CAR.KIA_EV9 and CP.flags & HyundaiFlags.CANFD_ANGLE_STEERING
  if not applicable:
    return legacy_lka_icon, legacy_lfa_icon, None

  meaningfully_actuating = bool(lat_active and steering_active)
  if lat_active:
    icon = 2 if meaningfully_actuating else 1
    return icon, icon, meaningfully_actuating
  return 1, 0, False


def ev9_reconstructed_steering_available(lat_active: bool, controls_enabled: bool,
                                         always_on_lateral_enabled: bool) -> bool:
  return bool(lat_active or controls_enabled or always_on_lateral_enabled)


@dataclass
class EV9DashController:
  CP: object
  packer: object
  CAN: object
  _aol_stock_counter: int | None = None

  @property
  def enabled(self) -> bool:
    return self.CP.carFingerprint == CAR.KIA_EV9

  def active(self, long_active_ecu: bool) -> bool:
    return bool(self.enabled and long_active_ecu)

  @staticmethod
  def main_mode(CS) -> bool:
    cruise_state = getattr(CS.out, "cruiseState", None)
    cruise_available = bool(getattr(cruise_state, "available", False))
    return bool(getattr(CS, "ev9_cruise_main_on", cruise_available)) and not getattr(CS.out, "accFaulted", False)

  def create_status_messages(self, frame: int, CC, CS, main_mode: bool,
                             steering_active: bool | None) -> list[CanData]:
    if frame % 5 != 0:
      return []
    return ev9_canfd.create_angle_long_status_messages(
      self.packer, self.CP, self.CAN, frame // 5, CC.enabled, main_mode,
      CC.hudControl, CS.out, CS.is_metric,
      steering_available=ev9_reconstructed_steering_available(
        CC.latActive, CC.enabled, bool(getattr(CS, "ev9_always_on_lateral_enabled", False)),
      ),
      steering_active=bool(steering_active),
      hba_icon=CS.hba_icon, dash_scene=getattr(CS, "ev9_dash_scene", None),
    )

  def create_adrv_messages(self, frame: int, CS, inputs, left_warning, right_warning) -> list[CanData]:
    messages = ev9_canfd.create_periodic_adrv_messages(self.CAN.ECAN, frame)

    if frame % 5 == 0:
      left_escalated = inputs.left_detected and inputs.left_stalk_active
      right_escalated = inputs.right_detected and inputs.right_stalk_active
      messages.extend(ev9_canfd.create_blindspot_status_messages(
        self.packer, self.CAN, frame // 5,
        inputs.left_detected, inputs.right_detected, left_escalated, right_escalated,
        CS.out.gearShifter == structs.CarState.GearShifter.drive,
        left_warning.mirror_lamp_active, right_warning.mirror_lamp_active,
        left_warning.sound_active, right_warning.sound_active,
      ))

    messages.append(ev9_canfd.create_accelerator_brake_alt(
      0, frame, CS.out.brakePressed, CS.out.gasPressed,
    ))
    return messages

  def create_aol_lateral_status(self, now_nanos: int, CS, steering_available: bool,
                                steering_active: bool, hud) -> list[CanData]:
    stock_values = getattr(CS, "msg_161", {})
    stock_timestamp = int(getattr(CS, "msg_161_ts", 0))
    stock_fresh = 0 <= now_nanos - stock_timestamp <= EV9_STOCK_STATUS_STALE_NS
    if not steering_available or not stock_values or not stock_fresh:
      self._aol_stock_counter = None
      return []

    stock_counter = int(stock_values["COUNTER"])
    if stock_counter == self._aol_stock_counter:
      return []
    self._aol_stock_counter = stock_counter
    return [ev9_canfd.create_aol_lateral_status(
      self.packer, self.CP, self.CAN, stock_values, steering_available, steering_active,
      hud, getattr(CS, "ev9_dash_scene", None),
    )]
