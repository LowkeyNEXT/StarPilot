from pathlib import Path
from types import SimpleNamespace

import pytest

from opendbc.car.disable_ecu import disable_ecu
from opendbc.car.hyundai.ev9_preinit import ev9_interface_init_profile
from opendbc.car.hyundai.ev9_preinit_controller import EV9PreinitController
from opendbc.car.hyundai.ev9_preinit_safety import EV9ActuationAbortReason, ev9_actuation_abort_reason, \
                                                          ev9_direct_angle_request_allowed
from opendbc.car.hyundai.values import CAR


class FakeParams:
  def __init__(self, values=None):
    self.values = values or {}

  def get_bool(self, key):
    return bool(self.values.get(key, False))


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


def test_production_module_has_no_probe_or_tuning_surface():
  source = Path(__file__).parents[1].joinpath("ev9_preinit_safety.py").read_text()
  for forbidden in ("TestStage", "ProbeMode", "DtcCapture", "DTC_CAPTURE", "get_ev9_longitudinal_test_config"):
    assert forbidden not in source
  for forbidden in ("jerk", "rate_limit", "tuning"):
    assert forbidden not in source.lower()


def test_preinit_is_inert_without_explicit_toggle_and_runtime_owner():
  CP = SimpleNamespace(carFingerprint=CAR.KIA_EV9, openpilotLongitudinalControl=True)
  profile = ev9_interface_init_profile(CP, FakeParams(), normal_init=True)
  controller = EV9PreinitController(CP, None, None)

  assert not profile.enabled
  assert not controller.active(True, SimpleNamespace(ev9_preinit_active=False))
  assert controller.active(True, SimpleNamespace(ev9_preinit_active=True))


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
  from opendbc.car.hyundai import ev9_preinit_canfd

  ev9_preinit_canfd.set_adrv_baselines([])
  messages = ev9_preinit_canfd.create_periodic_adrv_messages(1, 100)
  assert [msg.address for msg in messages] == [0x160, 0x1EA, 0x200, 0x345, 0x1DA, 0x1E0, 0x38C]
  assert [msg.dat[2] for msg in messages] == [50, 20, 20, 5, 1, 20, 5]


def test_direct_angle_command_requires_drive_and_lateral_request():
  assert ev9_direct_angle_request_allowed(True, True, True)
  assert not ev9_direct_angle_request_allowed(False, True, True)
  assert not ev9_direct_angle_request_allowed(True, False, True)
  assert not ev9_direct_angle_request_allowed(True, True, False)
  assert ev9_direct_angle_request_allowed(True, True, False, steer_at_standstill=True)


def test_actuation_abort_gate_inhibits_each_integrity_fault():
  assert ev9_actuation_abort_reason(False, False, False, True) == EV9ActuationAbortReason.NONE
  healthy = dict(control_requested=True, can_valid=True, radar_valid=True, panda_faulted=False)
  assert ev9_actuation_abort_reason(**healthy) == EV9ActuationAbortReason.NONE
  assert ev9_actuation_abort_reason(**(healthy | {"can_valid": False})) == EV9ActuationAbortReason.CAN_INVALID
  assert ev9_actuation_abort_reason(**(healthy | {"radar_valid": False})) == EV9ActuationAbortReason.RADAR_INVALID
  assert ev9_actuation_abort_reason(**(healthy | {"panda_faulted": True})) == EV9ActuationAbortReason.PANDA_FAULT
  assert ev9_actuation_abort_reason(**(healthy | {"scc_baseline_valid": False})) == \
    EV9ActuationAbortReason.STOCK_SCC_BASELINE_MISSING
