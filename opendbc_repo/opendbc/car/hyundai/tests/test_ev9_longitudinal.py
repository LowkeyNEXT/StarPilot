from pathlib import Path

import pytest

from opendbc.car.disable_ecu import disable_ecu
from opendbc.car.hyundai.carcontroller import EV9_STANDSTILL_DELAY_FRAMES, EV9_STOP_RELEASE_DELAY_FRAMES, \
                                               EV9LongitudinalTuningState, update_ev9_longitudinal_tuning
from opendbc.car.hyundai.ev9_longitudinal import EV9_PRODUCTION_REPLAY_ADDRS, \
                                                    EV9ActuationAbortReason, \
                                                    ev9_actuation_abort_reason, ev9_limit_stopping_accel, \
                                                    ev9_longitudinal_scc_command, filter_ev9_adrv_replay_messages, \
                                                    shape_ev9_longitudinal_accel, \
                                                    should_send_ev9_direct_angle_command


@pytest.mark.parametrize(("uds_request", "cc_response", "require_response", "expected"), (
  (b"\x28\x01\x01", {}, False, True),
  (b"\x28\x01\x01", {}, True, False),
  (b"\x28\x01\x01", {(0x738, None): b"\x68\x01"}, True, True),
  (b"\x28\x81\x01", {}, False, True),
))
def test_disable_ecu_requires_positive_response_only_when_requested(monkeypatch, uds_request, cc_response,
                                                                    require_response, expected):
  responses = iter(({(0x738, None): b""}, cc_response))
  requests = []

  class FakeIsoTpParallelQuery:
    def __init__(self, _can_send, _can_recv, _bus, _addrs, uds_requests, _uds_responses):
      requests.append(uds_requests[0])

    def get_data(self, _timeout):
      return next(responses)

  monkeypatch.setattr("opendbc.car.disable_ecu.IsoTpParallelQuery", FakeIsoTpParallelQuery)
  monkeypatch.setattr("opendbc.car.disable_ecu.time.sleep", lambda _seconds: None)

  assert disable_ecu(None, None, bus=1, addr=0x730, com_cont_req=uds_request, retry=1, session_delay=0.0,
                     require_response=require_response) is expected
  assert requests == [b"\x10\x03", uds_request]


def test_production_module_has_no_stage_probe_or_dtc_runtime_surface():
  source = Path(__file__).parents[1].joinpath("ev9_longitudinal.py").read_text()
  for forbidden in ("TestStage", "ProbeMode", "DtcCapture", "DTC_CAPTURE", "get_ev9_longitudinal_test_config"):
    assert forbidden not in source


def test_upstream_helpers_remain_in_their_original_modules():
  from opendbc.car.hyundai import carcontroller, ev9_preinit, radar_interface

  assert carcontroller.EV9LongitudinalTuningState.__module__ == carcontroller.__name__
  assert carcontroller.update_ev9_longitudinal_tuning.__module__ == carcontroller.__name__
  assert carcontroller.BlindspotWarningState.__module__ == carcontroller.__name__
  for name in (
    "EV9PandaPreinitHandoff", "EV9_PANDA_PREINIT_HANDOFF",
    "attempt_ev9_pre_fingerprint_suppression", "update_ev9_panda_preinit_handoff",
  ):
    assert getattr(ev9_preinit, name) is not None
  assert radar_interface.RadarInterface.__module__ == radar_interface.__name__


def test_ev9_periodic_adrv_schedule_is_owned_and_stable():
  from opendbc.car.hyundai import ev9_canfd

  ev9_canfd.set_adrv_baselines([])
  messages = ev9_canfd.create_periodic_adrv_messages(1, 100)
  assert [msg.address for msg in messages] == [0x160, 0x1EA, 0x200, 0x345, 0x1DA, 0x1E0, 0x38C]
  assert [msg.dat[2] for msg in messages] == [50, 20, 20, 5, 1, 20, 5]


def test_adrv_replay_is_fixed_and_drops_0x51_and_physical_0x57a():
  messages = [(addr, b"", 1) for addr in (0x51, 0x57A, 0x160, 0x1DA, 0x1EA, 0x200, 0x345)]
  filtered = filter_ev9_adrv_replay_messages(messages)
  assert [m[0] for m in filtered] == [0x160, 0x1DA, 0x1EA, 0x200, 0x345]
  assert {m[0] for m in filtered} == EV9_PRODUCTION_REPLAY_ADDRS


def test_direct_angle_command_requires_drive_and_lateral_request():
  assert should_send_ev9_direct_angle_command(True, True, True)
  assert not should_send_ev9_direct_angle_command(False, True, True)
  assert not should_send_ev9_direct_angle_command(True, False, True)
  assert not should_send_ev9_direct_angle_command(True, True, False)
  assert should_send_ev9_direct_angle_command(True, True, False, steer_at_standstill=True)


def test_scc_command_clamps_and_fails_closed():
  assert ev9_longitudinal_scc_command(True, -4.0, True, True) == (True, -3.5, True, True)
  assert ev9_longitudinal_scc_command(True, 4.0, False, False) == (True, 3.5, False, False)
  assert ev9_longitudinal_scc_command(True, 0.1, False, False, False) == (False, 0.0, False, False)


def test_ev9_accel_value_uses_normal_and_comfort_launch_ramps():
  normal = shape_ev9_longitudinal_accel(0.0, 0.18, 10.0, False, False, EV9LongitudinalTuningState())
  launch = shape_ev9_longitudinal_accel(0.0, 0.20, 0.0, True, False, EV9LongitudinalTuningState())
  assert normal == pytest.approx((0.18, 0.014, 0.7))
  assert launch == pytest.approx((0.20, 0.01, 0.5))


def test_ev9_stock_route_stop_taper_caps_only_excessive_braking():
  assert ev9_limit_stopping_accel(-3.0, 4.0) == -2.20
  assert ev9_limit_stopping_accel(-3.0, 2.0) == -1.65
  assert ev9_limit_stopping_accel(-2.0, 1.0) == -1.05
  assert ev9_limit_stopping_accel(-0.5, 1.0) == -0.5
  assert ev9_limit_stopping_accel(0.2, 1.0) == 0.0


def test_ev9_selects_distinct_normal_launch_stop_and_hold_jerk():
  initial_stop = EV9LongitudinalTuningState(stop_request=True)
  pre_standstill = EV9LongitudinalTuningState(stop_request=True, stop_request_frames=1)
  standstill = EV9LongitudinalTuningState(stop_request=True, cruise_standstill=True)
  release = EV9LongitudinalTuningState(stop_request=True, release_frames=1)

  assert shape_ev9_longitudinal_accel(-0.7, -0.7, 0.46, False, True, initial_stop) == (0.0, 0.0, 1.0)
  assert shape_ev9_longitudinal_accel(0.0, 0.0, 0.45, False, True, pre_standstill) == (0.0, 0.0, 1.5)
  assert shape_ev9_longitudinal_accel(0.0, 0.0, 0.0, False, True, standstill) == (0.0, 0.0, 1.5)
  assert shape_ev9_longitudinal_accel(0.0, 0.2, 0.0, True, False, release) == (0.0, 0.0, 1.5)


def test_ev9_stop_hold_and_release_match_stock_route_timing():
  state = EV9LongitudinalTuningState()
  assert not update_ev9_longitudinal_tuning(state, True, True, 0.48).stop_request

  state = update_ev9_longitudinal_tuning(state, True, True, 0.47)
  assert state.stop_request and state.stop_request_frames == 0
  for _ in range(EV9_STANDSTILL_DELAY_FRAMES):
    state = update_ev9_longitudinal_tuning(state, True, True, 0.0)
  assert state.cruise_standstill

  for release_frame in range(1, EV9_STOP_RELEASE_DELAY_FRAMES + 1):
    state = update_ev9_longitudinal_tuning(state, True, False, 0.0)
    assert state.stop_request and state.release_frames == release_frame
  assert update_ev9_longitudinal_tuning(state, True, False, 0.0) == EV9LongitudinalTuningState()


def test_actuation_abort_gate_inhibits_each_integrity_fault():
  assert ev9_actuation_abort_reason(False, False, False, True) == EV9ActuationAbortReason.NONE
  healthy = dict(control_requested=True, can_valid=True, radar_valid=True, panda_faulted=False)
  assert ev9_actuation_abort_reason(**healthy) == EV9ActuationAbortReason.NONE
  assert ev9_actuation_abort_reason(**(healthy | {"can_valid": False})) == EV9ActuationAbortReason.CAN_INVALID
  assert ev9_actuation_abort_reason(**(healthy | {"radar_valid": False})) == EV9ActuationAbortReason.RADAR_INVALID
  assert ev9_actuation_abort_reason(**(healthy | {"panda_faulted": True})) == EV9ActuationAbortReason.PANDA_FAULT
  assert ev9_actuation_abort_reason(**(healthy | {"scc_baseline_valid": False})) == \
    EV9ActuationAbortReason.STOCK_SCC_BASELINE_MISSING
