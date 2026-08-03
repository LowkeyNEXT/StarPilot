from dataclasses import dataclass

from opendbc.car import CanData, structs
from opendbc.car.carlog import carlog
from opendbc.car.hyundai import ev9_preinit_canfd
from opendbc.car.hyundai.ev9_preinit_safety import EV9ActuationAbortReason, ev9_actuation_abort_reason, \
                                                      ev9_direct_angle_request_allowed
from opendbc.car.hyundai.values import CAR


@dataclass
class EV9PreinitController:
  CP: object
  packer: object
  CAN: object
  actuation_fault_reason: EV9ActuationAbortReason = EV9ActuationAbortReason.NONE
  scc_counter: int = 0

  @property
  def enabled(self) -> bool:
    return self.CP.carFingerprint == CAR.KIA_EV9

  def active(self, long_active_ecu: bool, CS=None) -> bool:
    return bool(self.enabled and long_active_ecu and CS is not None and getattr(CS, "ev9_preinit_active", False))

  @staticmethod
  def main_mode(CS) -> bool:
    cruise_state = getattr(CS.out, "cruiseState", None)
    cruise_available = bool(getattr(cruise_state, "available", False))
    return bool(getattr(CS, "ev9_cruise_main_on", cruise_available)) and not getattr(CS.out, "accFaulted", False)

  def inhibit_faulted_steering(self, CS, long_active_ecu: bool,
                               apply_torque: float, apply_angle: float) -> tuple[float, float]:
    if self.active(long_active_ecu, CS) and bool(getattr(CS, "panda_faulted", True)):
      return 0.0, float(getattr(CS, "angle_steering_angle", CS.out.steeringAngleDeg))
    return apply_torque, apply_angle

  def actuation_permitted(self, frame: int, CC, CS, requested_accel: float) -> bool:
    abort_reason = ev9_actuation_abort_reason(
      bool(CC.enabled or CC.latActive),
      bool(CS.out.canValid),
      bool(getattr(CS, "openpilot_radar_valid", False)),
      bool(getattr(CS, "panda_faulted", True)),
      ev9_preinit_canfd.scc_control_baseline_available(),
    )
    permitted = abort_reason == EV9ActuationAbortReason.NONE
    if abort_reason != self.actuation_fault_reason:
      if abort_reason == EV9ActuationAbortReason.NONE:
        carlog.warning(f"EV9 ACTUATION INHIBIT CLEARED: {self.actuation_fault_reason.name}")
      else:
        carlog.error(f"EV9 ACTUATION INHIBITED: {abort_reason.name}")
      self.actuation_fault_reason = abort_reason
    if frame % 100 == 0:
      carlog.warning("".join((
        f"EV9 ACTUATION: permitted={permitted}, enabled={CC.enabled}, ",
        f"requestedAccel={requested_accel:.3f}, vEgo={CS.out.vEgo:.3f}",
      )))
    return permitted

  def create_steering_messages(self, frame: int, CC, CS, apply_torque: float,
                               apply_angle: float, actuation_permitted: bool) -> list[CanData]:
    measured_angle = float(getattr(CS, "angle_steering_angle", CS.out.steeringAngleDeg))
    inactive_steering = ev9_preinit_canfd.create_inactive_steering_messages(
      self.packer, self.CAN, measured_angle, frame,
    )
    messages = [inactive_steering[0]]
    drive_gear = CS.out.gearShifter == structs.CarState.GearShifter.drive
    if drive_gear:
      direct_active = ev9_direct_angle_request_allowed(
        drive_gear, CC.latActive, not bool(getattr(CS.out, "standstill", False)), self.CP.steerAtStandstill,
      ) and actuation_permitted and not getattr(CS, "angle_steering_fault", False)
      messages.append(ev9_preinit_canfd.create_direct_angle_command(
        self.packer, self.CAN, apply_angle if direct_active else measured_angle,
        direct_active, apply_torque if direct_active else 0.0, frame,
      ))
    else:
      messages.append(inactive_steering[1])
    return messages

  def create_status_messages(self, frame: int, CC, CS, main_mode: bool,
                             steering_active: bool, actuation_permitted: bool) -> list[CanData]:
    if frame % 5 != 0:
      return []
    steering_available = bool(CC.latActive or CC.enabled or getattr(CS, "ev9_always_on_lateral_enabled", False))
    return ev9_preinit_canfd.create_basic_status_messages(
      self.packer, self.CAN, frame // 5, CC.enabled, main_mode,
      CC.hudControl, CS.out, CS.is_metric, steering_available,
      bool(steering_active and actuation_permitted), CS.hba_icon,
    )

  def create_adrv_messages(self, frame: int, CS) -> list[CanData]:
    messages = ev9_preinit_canfd.create_periodic_adrv_messages(self.CAN.ECAN, frame)
    if frame % 5 == 0:
      messages.extend(ev9_preinit_canfd.create_neutral_blindspot_messages(
        self.packer, self.CAN, frame // 5,
        CS.out.gearShifter == structs.CarState.GearShifter.drive,
      ))
    messages.append(ev9_preinit_canfd.create_accelerator_brake_alt(
      0, frame, CS.out.brakePressed, CS.out.gasPressed,
    ))
    return messages

  def create_acc_control(self, CC, CS, requested_accel: float,
                         set_speed_in_units: float, main_mode: bool, lead_visible: bool,
                         lead_distance: float, lead_rel_speed: float,
                         actuation_permitted: bool, stop_state,
                         jerk_lower: float, jerk_upper: float) -> tuple[CanData, float]:
    enabled = bool(CC.enabled and actuation_permitted)
    gas_override = bool(CC.cruiseControl.override)
    accel_value = requested_accel if enabled and not gas_override and not stop_state.stop_request else 0.0

    message = ev9_preinit_canfd.create_acc_control(
      self.packer, self.CAN, self.scc_counter, enabled, accel_value, accel_value,
      stop_state.stop_request, stop_state.cruise_standstill, gas_override,
      set_speed_in_units, int(main_mode), lead_distance, lead_rel_speed, lead_visible, float(CS.out.vEgo),
      jerk_lower=jerk_lower, jerk_upper=jerk_upper,
    )
    self.scc_counter = (self.scc_counter + 1) & 0xFF
    return message, accel_value
