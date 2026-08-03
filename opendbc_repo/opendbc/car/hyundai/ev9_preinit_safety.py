from enum import IntEnum


class EV9ActuationAbortReason(IntEnum):
  NONE = 0
  CAN_INVALID = 1
  RADAR_INVALID = 2
  PANDA_FAULT = 3
  STOCK_SCC_BASELINE_MISSING = 4


def ev9_direct_angle_request_allowed(drive_gear: bool, lat_active: bool,
                                     vehicle_moving: bool, steer_at_standstill: bool = False) -> bool:
  """Match the CCNC angle-long safety gate for an active 0xCB request."""
  return drive_gear and lat_active and (vehicle_moving or steer_at_standstill)


def ev9_actuation_abort_reason(control_requested: bool, can_valid: bool, radar_valid: bool,
                               panda_faulted: bool, scc_baseline_valid: bool = True) -> EV9ActuationAbortReason:
  """Inhibit EV9 actuation while any required ownership input is invalid."""
  if not control_requested:
    return EV9ActuationAbortReason.NONE
  if not can_valid:
    return EV9ActuationAbortReason.CAN_INVALID
  if not radar_valid:
    return EV9ActuationAbortReason.RADAR_INVALID
  if panda_faulted:
    return EV9ActuationAbortReason.PANDA_FAULT
  if not scc_baseline_valid:
    return EV9ActuationAbortReason.STOCK_SCC_BASELINE_MISSING
  return EV9ActuationAbortReason.NONE
