from types import SimpleNamespace

import pytest

from opendbc.car.hyundai.carcontroller import BlindspotWarningState, get_ev9_blindspot_warning_inputs, update_blindspot_warning
from opendbc.car.hyundai.carstate import CANFD_NATIVE_BLINDSPOT_STALE_NS, decode_canfd_blinker_stalks, \
                                           resolve_canfd_native_blindspot_state


def test_canfd_blinker_stalks_use_physical_0x413_bits():
  assert decode_canfd_blinker_stalks(1, 0) == (True, False)
  assert decode_canfd_blinker_stalks(0, 1) == (False, True)
  assert decode_canfd_blinker_stalks(0, 0) == (False, False)
  assert decode_canfd_blinker_stalks(2, 2) == (False, False)


@pytest.mark.parametrize("state", [1, 2])
def test_native_blindspot_accepts_valid_0x1ba_lamp_states(state):
  timestamp_nanos = 1_000_000_000
  assert resolve_canfd_native_blindspot_state(
    state, 0, timestamp_nanos, timestamp_nanos + CANFD_NATIVE_BLINDSPOT_STALE_NS,
  ) == (True, False, True)


@pytest.mark.parametrize("timestamp_nanos,now_nanos", [
  (0, 0),
  (1_000_000_000, 999_999_999),
  (1_000_000_000, 1_000_000_000 + CANFD_NATIVE_BLINDSPOT_STALE_NS + 1),
])
def test_native_blindspot_fails_neutral_when_missing_or_stale(timestamp_nanos, now_nanos):
  assert resolve_canfd_native_blindspot_state(1, 2, timestamp_nanos, now_nanos) == (False, False, False)


def test_native_blindspot_rejects_unvalidated_state_three():
  assert resolve_canfd_native_blindspot_state(3, 3, 1_000_000_000, 1_000_000_000) == (False, False, True)


def test_ev9_warning_uses_native_lamp_and_physical_stalk():
  cs = SimpleNamespace(
    native_left_blindspot_state=1,
    native_right_blindspot_state=0,
    native_blindspot_ts=1_000_000_000,
    left_blinker_stalk=True,
    right_blinker_stalk=False,
    # The retained 0x36A proxy must not influence a native warning decision.
    left_blindspot_from_radar=False,
    right_blindspot_from_radar=True,
  )
  inputs = get_ev9_blindspot_warning_inputs(cs, 1_050_000_000)
  assert inputs.source_fresh
  assert inputs.left_detected and inputs.left_stalk_active
  assert not inputs.right_detected and not inputs.right_stalk_active


def test_ev9_warning_ignores_raw_proxy_without_native_0x1ba():
  cs = SimpleNamespace(
    native_left_blindspot_state=0,
    native_right_blindspot_state=0,
    native_blindspot_ts=0,
    left_blinker_stalk=True,
    right_blinker_stalk=False,
    left_blindspot_from_radar=True,
    right_blindspot_from_radar=False,
  )
  assert get_ev9_blindspot_warning_inputs(cs, 1_000_000_000).source_fresh is False
  assert get_ev9_blindspot_warning_inputs(cs, 1_000_000_000).left_detected is False


def test_ev9_warning_suppresses_ambiguous_dual_stalk_state():
  cs = SimpleNamespace(
    native_left_blindspot_state=1,
    native_right_blindspot_state=2,
    native_blindspot_ts=1_000_000_000,
    left_blinker_stalk=True,
    right_blinker_stalk=True,
  )
  inputs = get_ev9_blindspot_warning_inputs(cs, 1_000_000_000)
  assert inputs.left_detected and inputs.right_detected
  assert not inputs.left_stalk_active and not inputs.right_stalk_active


def test_stale_native_state_hard_resets_warning_envelope():
  state = BlindspotWarningState()
  assert update_blindspot_warning(state, escalated=True, blinker=True).sound_active

  cs = SimpleNamespace(
    native_left_blindspot_state=2,
    native_right_blindspot_state=0,
    native_blindspot_ts=1_000_000_000,
    left_blinker_stalk=True,
    right_blinker_stalk=False,
  )
  inputs = get_ev9_blindspot_warning_inputs(
    cs, 1_000_000_000 + CANFD_NATIVE_BLINDSPOT_STALE_NS + 1,
  )
  output = update_blindspot_warning(
    state, escalated=inputs.left_detected and inputs.left_stalk_active,
    blinker=inputs.left_stalk_active,
  )
  assert not output.mirror_lamp_active
  assert not output.sound_active
