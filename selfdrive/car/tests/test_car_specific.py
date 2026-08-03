from cereal import car, log
from opendbc.car.hyundai.values import CAR as HYUNDAI_CAR

from openpilot.selfdrive.car.car_specific import CarSpecificEvents
from openpilot.selfdrive.car.cruise_state import should_cancel_stock_cruise, should_flag_cruise_mismatch

EventName = log.OnroadEvent.EventName


def make_cp(brand="hyundai", op_long=True, pcm_cruise=False):
  cp = car.CarParams.new_message()
  cp.brand = brand
  cp.openpilotLongitudinalControl = op_long
  cp.pcmCruise = pcm_cruise
  return cp


def test_hyundai_openpilot_long_does_not_cancel_active_acc_req_feedback():
  cp = make_cp()

  assert not should_cancel_stock_cruise(cp, cruise_enabled=True, controls_enabled=True)
  assert not should_flag_cruise_mismatch(cp, cruise_enabled=True, controls_enabled=True, effective_pcm_cruise=False)


def test_hyundai_openpilot_long_still_flags_cruise_when_controls_disabled():
  cp = make_cp()

  assert should_cancel_stock_cruise(cp, cruise_enabled=True, controls_enabled=False)
  assert should_flag_cruise_mismatch(cp, cruise_enabled=True, controls_enabled=False, effective_pcm_cruise=False)


def test_non_hyundai_openpilot_long_behavior_is_unchanged():
  cp = make_cp(brand="toyota")

  assert should_cancel_stock_cruise(cp, cruise_enabled=True, controls_enabled=True)
  assert should_flag_cruise_mismatch(cp, cruise_enabled=True, controls_enabled=True, effective_pcm_cruise=False)


def test_pcm_cruise_behavior_is_unchanged():
  cp = make_cp(op_long=False, pcm_cruise=True)

  assert not should_cancel_stock_cruise(cp, cruise_enabled=True, controls_enabled=True)
  assert should_cancel_stock_cruise(cp, cruise_enabled=True, controls_enabled=False)
  assert not should_flag_cruise_mismatch(cp, cruise_enabled=True, controls_enabled=True, effective_pcm_cruise=True)
  assert should_flag_cruise_mismatch(cp, cruise_enabled=True, controls_enabled=False, effective_pcm_cruise=True)


def test_adas_unavailable_has_dedicated_event_without_faking_can_invalid():
  cp = make_cp()
  cp.carFingerprint = HYUNDAI_CAR.KIA_EV9
  events = CarSpecificEvents(cp)
  cs = car.CarState.new_message()
  cs_prev = car.CarState.new_message()
  cc = car.CarControl.new_message()
  cs.gearShifter = car.CarState.GearShifter.drive
  cs.cruiseState.available = True
  cs.canValid = True
  cs.adasUnavailable = True

  names = events.update(cs.as_reader(), cs_prev.as_reader(), cc.as_reader()).names

  assert EventName.adasUnavailable in names
  assert cs.canValid
