from dataclasses import dataclass, field

from opendbc.car import CanData, structs
from opendbc.car.carlog import carlog
from opendbc.car.hyundai import ev9_canfd
from opendbc.car.hyundai.ev9_bsm import BlindspotWarningOutput as BlindspotWarningOutput, \
                                          BlindspotWarningState as BlindspotWarningState, \
                                          EV9BlindspotWarningInputs as EV9BlindspotWarningInputs, \
                                          get_blindspot_warning_inputs as get_blindspot_warning_inputs, \
                                          get_ev9_blindspot_warning_inputs as get_ev9_blindspot_warning_inputs, \
                                          update_blindspot_warning as update_blindspot_warning
from opendbc.car.hyundai.ev9_longitudinal import EV9_ACTUATION_JERK_LOWER, EV9_ACTUATION_JERK_UPPER, \
                                                   EV9ActuationAbortReason, EV9LongitudinalStopState, \
                                                   ev9_actuation_abort_reason, ev9_longitudinal_scc_command, \
                                                   shape_ev9_longitudinal_accel, \
                                                   should_send_ev9_direct_angle_command, update_ev9_longitudinal_stop_state
from opendbc.car.hyundai.values import CAR, HyundaiFlags


def ev9_dynamic_steering_icons(CP, lat_active: bool, steering_active: bool,
                               legacy_lka_icon: int, legacy_lfa_icon: int,
                               downstream_angle_command_available: bool = True) -> tuple[int, int, bool | None]:
  applicable = CP.carFingerprint == CAR.KIA_EV9 and CP.flags & HyundaiFlags.CANFD_ANGLE_STEERING
  if not applicable:
    return legacy_lka_icon, legacy_lfa_icon, None

  meaningfully_actuating = bool(lat_active and steering_active and downstream_angle_command_available)
  if lat_active:
    icon = 2 if meaningfully_actuating else 1
    return icon, icon, meaningfully_actuating
  return 1, 0, False


def ev9_reconstructed_steering_available(lat_active: bool, controls_enabled: bool,
                                         always_on_lateral_enabled: bool) -> bool:
  return bool(lat_active or controls_enabled or always_on_lateral_enabled)


@dataclass
class EV9Controller:
  CP: object
  packer: object
  CAN: object
  actuation_fault_reason: EV9ActuationAbortReason = EV9ActuationAbortReason.NONE
  scc_counter: int = 0
  stop_state: EV9LongitudinalStopState = field(default_factory=EV9LongitudinalStopState)
  left_blindspot_warning: BlindspotWarningState = field(default_factory=BlindspotWarningState)
  right_blindspot_warning: BlindspotWarningState = field(default_factory=BlindspotWarningState)

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

  def direct_path_available(self, CC, CS, long_active_ecu: bool) -> bool:
    if not self.active(long_active_ecu):
      return True
    return should_send_ev9_direct_angle_command(
      CS.out.gearShifter == structs.CarState.GearShifter.drive, CC.latActive,
      not bool(getattr(CS.out, "standstill", False)), self.CP.steerAtStandstill,
    ) and not getattr(CS, "angle_steering_fault", False)

  def inhibit_faulted_steering(self, CS, long_active_ecu: bool,
                               apply_torque: float, apply_angle: float) -> tuple[float, float]:
    if self.active(long_active_ecu) and bool(getattr(CS, "panda_faulted", True)):
      return 0.0, float(getattr(CS, "angle_steering_angle", CS.out.steeringAngleDeg))
    return apply_torque, apply_angle

  def actuation_permitted(self, frame: int, CC, CS, requested_accel: float) -> bool:
    abort_reason = ev9_actuation_abort_reason(
      bool(CC.enabled or CC.latActive),
      bool(CS.out.canValid),
      bool(getattr(CS, "openpilot_radar_valid", False)),
      bool(getattr(CS, "panda_faulted", True)),
      ev9_canfd.scc_control_baseline_available(),
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
    inactive_steering = ev9_canfd.create_inactive_steering_messages(
      self.packer, self.CAN, measured_angle, frame,
    )
    messages = [inactive_steering[0]]
    drive_gear = CS.out.gearShifter == structs.CarState.GearShifter.drive
    if drive_gear:
      direct_active = should_send_ev9_direct_angle_command(
        drive_gear, CC.latActive, not bool(getattr(CS.out, "standstill", False)), self.CP.steerAtStandstill,
      ) and actuation_permitted and not getattr(CS, "angle_steering_fault", False)
      messages.append(ev9_canfd.create_direct_angle_command(
        self.packer, self.CAN, apply_angle if direct_active else measured_angle,
        direct_active, apply_torque if direct_active else 0.0, frame,
      ))
    else:
      messages.append(inactive_steering[1])
    return messages

  def create_dash_messages(self, frame: int, CC, CS, main_mode: bool,
                           steering_active: bool, actuation_permitted: bool) -> list[CanData]:
    if frame % 5 != 0:
      return []
    return ev9_canfd.create_angle_long_status_messages(
      self.packer, self.CP, self.CAN, frame // 5, CC.enabled, main_mode,
      CC.hudControl, CS.out, CS.is_metric,
      steering_available=ev9_reconstructed_steering_available(
        CC.latActive, CC.enabled, bool(getattr(CS, "ev9_always_on_lateral_enabled", False)),
      ),
      steering_active=bool(steering_active and actuation_permitted),
      hba_icon=CS.hba_icon, dash_scene=getattr(CS, "ev9_dash_scene", None),
    )

  def create_adrv_messages(self, frame: int, now_nanos: int, CS) -> list[CanData]:
    messages = ev9_canfd.create_periodic_adrv_messages(self.CAN.ECAN, frame)

    if frame % 5 == 0:
      inputs = get_ev9_blindspot_warning_inputs(CS, now_nanos)
      left_escalated = inputs.left_detected and inputs.left_stalk_active
      right_escalated = inputs.right_detected and inputs.right_stalk_active
      left_warning = update_blindspot_warning(self.left_blindspot_warning, left_escalated, inputs.left_stalk_active)
      right_warning = update_blindspot_warning(self.right_blindspot_warning, right_escalated, inputs.right_stalk_active)
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

  def create_acc_control(self, CC, CS, accel_last: float, requested_accel: float, stopping: bool,
                         set_speed_in_units: float, main_mode: bool, lead_visible: bool,
                         lead_distance: float, lead_rel_speed: float,
                         actuation_permitted: bool) -> tuple[CanData, float]:
    enabled, accel, stopping, gas_override = ev9_longitudinal_scc_command(
      CC.enabled, requested_accel, stopping, CC.cruiseControl.override,
      actuation_permitted=actuation_permitted,
    )
    self.stop_state = update_ev9_longitudinal_stop_state(
      self.stop_state, enabled and not gas_override, stopping, float(CS.out.vEgo),
    )
    starting = CC.actuators.longControlState == structs.CarControl.Actuators.LongControlState.starting
    if enabled and not gas_override:
      accel_raw, accel_value, jerk_upper = shape_ev9_longitudinal_accel(
        accel_last, accel, float(CS.out.vEgo), starting, stopping, self.stop_state,
      )
    else:
      accel_raw = 0.0
      accel_value = 0.0
      jerk_upper = EV9_ACTUATION_JERK_UPPER

    message = ev9_canfd.create_acc_control(
      self.packer, self.CAN, self.scc_counter, enabled, accel_raw, accel_value,
      self.stop_state.stop_request, self.stop_state.cruise_standstill, gas_override,
      set_speed_in_units, int(main_mode), lead_distance, lead_rel_speed, lead_visible, float(CS.out.vEgo),
      jerk_lower=EV9_ACTUATION_JERK_LOWER, jerk_upper=jerk_upper,
    )
    self.scc_counter = (self.scc_counter + 1) & 0xFF
    return message, accel_value


# Transitional names used by the first extraction.
dynamic_steering_icons = ev9_dynamic_steering_icons
reconstructed_steering_available = ev9_reconstructed_steering_available
