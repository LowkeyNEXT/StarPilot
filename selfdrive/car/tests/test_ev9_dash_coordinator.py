import time
from types import SimpleNamespace

from opendbc.car.hyundai.ev9_dash import Ev9RawBlindspotGateState
from openpilot.selfdrive.car.ev9_dash_coordinator import EV9DashCoordinator


class FakeMemoryParams:
  def __init__(self, values):
    self.values = values

  def get(self, key):
    return self.values.get(key)


def card_process(memory_values, native_fresh=False, enhanced=True, reconstruction=True,
                 raw_state=0, raw_fresh=False, raw_timestamp=0):
  car_state = SimpleNamespace(
    native_blindspot_fresh=native_fresh,
    ev9_raw_blindspot_state=raw_state,
    ev9_raw_blindspot_fresh=raw_fresh,
    ev9_raw_blindspot_ts=raw_timestamp,
    ev9_reconstructed_left_blindspot=False,
    ev9_reconstructed_right_blindspot=False,
    ev9_reconstructed_blindspot_ts=0,
  )
  return SimpleNamespace(
    CP=SimpleNamespace(carFingerprint="KIA_EV9"),
    CI=SimpleNamespace(CS=car_state),
    ev9_bsm_reconstruction_enabled=reconstruction,
    ev9_enhanced_bsm_enabled=enhanced,
    ev9_raw_blindspot_gate=Ev9RawBlindspotGateState(),
    params_memory=FakeMemoryParams(memory_values),
  )


def test_fresh_vision_bsm_qualifies_same_side_raw_state_without_front_radar():
  timestamp = time.monotonic_ns()
  card = card_process({
    "VASMLastUpdateMonoTime": str(time.monotonic()),
    "VASMLeftActive": "1",
    "VASMRightActive": "0",
  }, raw_state=0x12, raw_fresh=True, raw_timestamp=timestamp)
  state = SimpleNamespace(leftBlindspot=False, rightBlindspot=False)

  EV9DashCoordinator._update_ev9_raw_blindspot_gate(card, state, None)

  assert state.leftBlindspot
  assert not state.rightBlindspot
  assert card.CI.CS.ev9_reconstructed_left_blindspot
  assert card.CI.CS.ev9_reconstructed_blindspot_ts > 0


def test_enhanced_bsm_rejects_vision_without_matching_raw_side_state():
  card = card_process({
    "VASMLastUpdateMonoTime": str(time.monotonic()),
    "VASMLeftActive": "1",
    "VASMRightActive": "0",
  })
  state = SimpleNamespace(leftBlindspot=False, rightBlindspot=False)

  EV9DashCoordinator._update_ev9_raw_blindspot_gate(card, state, None)

  assert not state.leftBlindspot
  assert not state.rightBlindspot


def test_stale_vision_bsm_fails_neutral():
  card = card_process({
    "VASMLastUpdateMonoTime": "1.0",
    "VASMLeftActive": "1",
    "VASMRightActive": "0",
  })
  state = SimpleNamespace(leftBlindspot=False, rightBlindspot=False)

  EV9DashCoordinator._update_ev9_raw_blindspot_gate(card, state, None)

  assert not state.leftBlindspot
  assert not state.rightBlindspot
  assert not card.CI.CS.ev9_reconstructed_left_blindspot
  assert card.CI.CS.ev9_reconstructed_blindspot_ts == 0


def test_fresh_native_bsm_remains_authoritative_over_vision():
  card = card_process({
    "VASMLastUpdateMonoTime": str(time.monotonic()),
    "VASMLeftActive": "1",
    "VASMRightActive": "0",
  }, native_fresh=True, raw_state=0x12, raw_fresh=True, raw_timestamp=time.monotonic_ns())
  state = SimpleNamespace(leftBlindspot=False, rightBlindspot=True)

  EV9DashCoordinator._update_ev9_raw_blindspot_gate(card, state, None)

  assert not state.leftBlindspot
  assert state.rightBlindspot


def test_basic_bsm_uses_raw_state_without_radar_or_vision_qualification():
  timestamp = time.monotonic_ns()
  card = card_process({
    "VASMLastUpdateMonoTime": str(time.monotonic()),
    "VASMLeftActive": "0",
    "VASMRightActive": "1",
  }, enhanced=False, raw_state=0x12, raw_fresh=True, raw_timestamp=timestamp)
  state = SimpleNamespace(leftBlindspot=False, rightBlindspot=False)

  EV9DashCoordinator._update_ev9_raw_blindspot_gate(card, state, None)

  assert state.leftBlindspot
  assert not state.rightBlindspot
  assert card.CI.CS.ev9_reconstructed_blindspot_ts == timestamp


def test_bsm_master_off_ignores_basic_raw_and_enhanced_vision_sources():
  card = card_process({
    "VASMLastUpdateMonoTime": str(time.monotonic()),
    "VASMLeftActive": "1",
    "VASMRightActive": "0",
  }, reconstruction=False, raw_state=0x12, raw_fresh=True, raw_timestamp=time.monotonic_ns())
  state = SimpleNamespace(leftBlindspot=False, rightBlindspot=False)

  EV9DashCoordinator._update_ev9_raw_blindspot_gate(card, state, None)

  assert not state.leftBlindspot
  assert not state.rightBlindspot
  assert card.CI.CS.ev9_reconstructed_blindspot_ts == 0
