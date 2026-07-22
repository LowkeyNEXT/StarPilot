from types import SimpleNamespace

import pytest

from opendbc.can import CANPacker, CANParser
from opendbc.car import Bus
from opendbc.car.structs import CarParams
from opendbc.car.hyundai import hyundaicanfd
from opendbc.car.hyundai.carstate import get_canfd_speed_limit_state
from opendbc.car.hyundai.ev9_dash import ClusterObject, ClusterObjectSlots, Ev9DashObjectTracker, Ev9DashScene, \
                                             Ev9LaneChangeAnimationState, display_context_valid, filter_side_objects, \
                                             radar_backed_object, select_lane_change_direction, select_stop_target, \
                                             update_lane_change_animation, validate_slots_for_output
from opendbc.car.hyundai.hyundaicanfd import CanBus
from opendbc.car.hyundai.radar_interface import ev9_dash_display_candidate, ev9_dash_side_candidate, \
                                                    ev9_dash_side_retention_candidate
from opendbc.car.hyundai.values import CAR, DBC, HyundaiFlags, HyundaiStarPilotFlags


def point(track_id=1, distance=30.0, lateral=0.0, relative_speed=-1.0, measured=True):
  return SimpleNamespace(trackId=track_id, dRel=distance, yRel=lateral,
                         vRel=relative_speed, measured=measured)


def lead(track_id=1, distance=30.0, lateral=0.0, relative_speed=-1.0,
         status=True, radar=True):
  return SimpleNamespace(status=status, radar=radar, radarTrackId=track_id,
                         dRel=distance, yRel=lateral, vRel=relative_speed)


def update(tracker, points, preferred=1, display=None, side=None, retention=None,
           v_ego=15.0, standstill=False, model_prob=1.0):
  display = {p.trackId for p in points} if display is None else display
  side = set() if side is None else side
  retention = set() if retention is None else retention
  return tracker.update(points, preferred, model_prob, display, side, retention, v_ego, standstill)


def acquire(tracker, points, samples=3, **kwargs):
  slots = ClusterObjectSlots()
  for _ in range(samples):
    slots = update(tracker, points, **kwargs)
  return slots


def test_display_context_requires_live_radar_main_and_drive():
  assert display_context_valid(True, True, True)
  assert not display_context_valid(False, True, True)
  assert not display_context_valid(True, False, True)
  assert not display_context_valid(True, True, False)


def test_tracker_requires_three_samples_and_smooths_distance():
  tracker = Ev9DashObjectTracker()
  assert update(tracker, [point(distance=30.0)]).primary is None
  assert update(tracker, [point(distance=32.0)]).primary is None
  primary = update(tracker, [point(distance=34.0)]).primary
  assert primary is not None
  assert primary.distance == pytest.approx(31.855)
  assert primary.distance < 34.0


def test_tracker_holds_four_dropouts_then_clears():
  tracker = Ev9DashObjectTracker()
  assert acquire(tracker, [point()]).primary is not None
  for _ in range(tracker.DROPOUT_HOLD_SAMPLES):
    assert update(tracker, [], display=set()).primary is not None
  assert update(tracker, [], display=set()).primary is None


def test_tracker_holds_qualified_side_slot_through_short_dropout():
  tracker = Ev9DashObjectTracker()
  left = point(track_id=2, distance=30.0, lateral=3.0, relative_speed=0.0)
  assert acquire(tracker, [left], preferred=-1, side={2}, retention={2}).left is not None
  for _ in range(tracker.DROPOUT_HOLD_SAMPLES):
    slots = update(tracker, [], preferred=-1, display=set(), side=set(), retention=set())
    assert slots.left is not None
  assert update(tracker, [], preferred=-1, display=set(), side=set(), retention=set()).left is None


def test_present_disqualified_track_clears_without_hold():
  tracker = Ev9DashObjectTracker()
  assert acquire(tracker, [point()]).primary is not None
  assert update(tracker, [point()], preferred=-1, display=set()).primary is None


def test_fused_primary_does_not_depend_on_route_variant_display_score():
  tracker = Ev9DashObjectTracker()
  slots = acquire(tracker, [point(track_id=7)], preferred=7, display=set())
  assert slots.primary is not None
  assert slots.primary.track_id == 7


def test_primary_requires_the_fused_radar_track():
  tracker = Ev9DashObjectTracker()
  points = [point(track_id=1, distance=20.0), point(track_id=2, distance=12.0)]
  slots = acquire(tracker, points, preferred=1)
  assert slots.primary is not None
  assert slots.primary.track_id == 1

  slots = update(tracker, points, preferred=-1)
  assert slots.primary is None


def test_primary_accepts_route_observed_lateral_without_old_2_2_cutoff():
  tracker = Ev9DashObjectTracker()
  slots = acquire(tracker, [point(lateral=2.4)], preferred=1, display=set())
  assert slots.primary is not None
  assert update(tracker, [point(lateral=2.6)], preferred=1, display=set()).primary is None


def test_primary_confidence_is_stricter_beyond_100_metres():
  near_tracker = Ev9DashObjectTracker()
  near = acquire(near_tracker, [point(distance=80.0)], preferred=1, display=set(), model_prob=0.8)
  assert near.primary is not None

  far_tracker = Ev9DashObjectTracker()
  far = acquire(far_tracker, [point(distance=120.0)], preferred=1, display=set(), model_prob=0.8, samples=10)
  assert far.primary is None
  far = acquire(far_tracker, [point(distance=120.0)], preferred=1, display=set(), model_prob=0.95)
  assert far.primary is not None


def test_primary_confidence_holds_four_low_probability_scans_then_clears():
  tracker = Ev9DashObjectTracker()
  assert acquire(tracker, [point()], preferred=1, display=set(), model_prob=1.0).primary is not None
  for _ in range(tracker.DROPOUT_HOLD_SAMPLES):
    assert update(tracker, [point()], preferred=1, display=set(), model_prob=0.5).primary is not None
  assert update(tracker, [point()], preferred=1, display=set(), model_prob=0.5).primary is None


def test_left_slot_is_stable_and_excludes_primary():
  tracker = Ev9DashObjectTracker()
  primary = point(track_id=1, distance=25.0, lateral=0.0, relative_speed=0.0)
  track_3 = point(track_id=3, distance=30.0, lateral=3.2, relative_speed=0.0)
  slots = acquire(tracker, [primary, track_3], side={3})
  assert slots.left is not None
  assert slots.left.track_id == 3

  moved = [primary, point(track_id=2, distance=29.0, lateral=3.0, relative_speed=0.0), track_3]
  slots = update(tracker, moved, side={2, 3})
  assert slots.left is not None
  assert slots.left.track_id == 3


def test_side_slot_does_not_acquire_from_ambiguous_candidates():
  tracker = Ev9DashObjectTracker()
  points = [
    point(track_id=2, distance=35.0, lateral=3.0, relative_speed=0.0),
    point(track_id=3, distance=30.0, lateral=3.2, relative_speed=0.0),
  ]
  assert acquire(tracker, points, preferred=-1, side={2, 3}).left is None


@pytest.mark.parametrize(("side", "adjacent_lateral"), [("left", 2.6), ("right", -2.8)])
def test_primary_moves_atomically_to_adjacent_slot(side, adjacent_lateral):
  tracker = Ev9DashObjectTracker()
  assert acquire(tracker, [point(track_id=7)], preferred=7, display=set()).primary is not None

  moved = point(track_id=7, lateral=adjacent_lateral, relative_speed=0.0)
  slots = update(tracker, [moved], preferred=-1, side={7}, retention={7})
  adjacent = getattr(slots, side)
  assert slots.primary is None
  assert adjacent is not None and adjacent.track_id == 7


def test_primary_handoff_wins_an_ambiguous_adjacent_scene_without_duplication():
  tracker = Ev9DashObjectTracker()
  primary = point(track_id=7, distance=30.0, relative_speed=0.0)
  adjacent = point(track_id=8, distance=40.0, lateral=3.2, relative_speed=0.0)
  slots = acquire(tracker, [primary, adjacent], preferred=7, side={8}, retention={8})
  assert slots.primary is not None and slots.primary.track_id == 7
  assert slots.left is not None and slots.left.track_id == 8

  crossing = point(track_id=7, distance=29.0, lateral=2.6, relative_speed=0.0)
  slots = update(tracker, [crossing, adjacent], preferred=-1, side={7, 8}, retention={7, 8})
  assert slots.primary is None
  assert slots.left is not None and slots.left.track_id == 7
  assert slots.left_rear is None


@pytest.mark.parametrize(("side", "side_lateral"), [("left", 3.0), ("right", -3.0)])
def test_adjacent_track_remains_visible_until_atomic_primary_promotion(side, side_lateral):
  tracker = Ev9DashObjectTracker()
  adjacent = point(track_id=7, lateral=side_lateral, relative_speed=0.0)
  slots = acquire(tracker, [adjacent], preferred=-1, side={7}, retention={7})
  assert getattr(slots, side) is not None

  entering = point(track_id=7, lateral=1.0 if side == "left" else -1.0, relative_speed=0.0)
  for _ in range(tracker.ACQUISITION_SAMPLES - 1):
    slots = update(tracker, [entering], preferred=7, side=set(), retention={7})
    assert slots.primary is None
    assert getattr(slots, side) is not None
  slots = update(tracker, [entering], preferred=7, side=set(), retention={7})
  assert slots.primary is not None and slots.primary.track_id == 7
  assert getattr(slots, side) is None


def test_right_slot_requires_deep_entry_then_retains_toward_lane_edge():
  tracker = Ev9DashObjectTracker()
  entering = point(track_id=4, distance=40.0, lateral=-3.2, relative_speed=0.0)
  slots = acquire(tracker, [entering], preferred=-1, side={4}, retention={4})
  assert slots.right is not None

  retained = point(track_id=4, distance=39.0, lateral=-1.7, relative_speed=0.0)
  slots = update(tracker, [retained], preferred=-1, side=set(), retention={4})
  assert slots.right is not None
  assert slots.right.track_id == 4


def test_right_slot_accepts_route_observed_inner_entry():
  tracker = Ev9DashObjectTracker()
  entering = point(track_id=4, distance=40.0, lateral=-2.4, relative_speed=0.0)
  slots = acquire(tracker, [entering], preferred=-1, side={4}, retention={4})
  assert slots.right is not None


def test_right_lane_edge_ghost_cannot_enter_without_deep_history():
  tracker = Ev9DashObjectTracker()
  ghost = point(track_id=4, distance=40.0, lateral=-1.7, relative_speed=0.0)
  slots = acquire(tracker, [ghost], preferred=-1, side={4}, retention={4}, samples=10)
  assert slots.right is None


def test_stationary_world_side_return_is_rejected():
  tracker = Ev9DashObjectTracker()
  wall = point(track_id=2, distance=20.0, lateral=3.0, relative_speed=-15.0)
  slots = acquire(tracker, [wall], preferred=-1, side={2}, retention={2}, v_ego=15.0)
  assert slots.left is None


def test_moving_side_target_is_retained_at_standstill():
  tracker = Ev9DashObjectTracker()
  moving = point(track_id=2, distance=20.0, lateral=3.0, relative_speed=-8.0)
  slots = acquire(tracker, [moving], preferred=-1, side={2}, retention={2}, v_ego=15.0)
  assert slots.left is not None

  stopped = point(track_id=2, distance=19.0, lateral=3.0, relative_speed=0.0)
  slots = update(tracker, [stopped], preferred=-1, side={2}, retention={2}, v_ego=0.0, standstill=True)
  assert slots.left is not None


def test_validate_output_rejects_stale_primary_and_stationary_side():
  slots = ClusterObjectSlots(
    primary=ClusterObject(1, 20.0, 0.0, 0.0),
    left=ClusterObject(2, 15.0, 3.0, -15.0, True, -15.0),
    left_rear=ClusterObject(4, 10.0, 3.0, -15.0, True, -15.0),
  )
  validated = validate_slots_for_output(slots, lead(track_id=3), 15.0, False)
  assert validated.primary is None
  assert validated.left is None
  assert validated.left_rear is None


def test_side_objects_fail_closed_unless_explicitly_enabled():
  slots = ClusterObjectSlots(
    primary=ClusterObject(1, 20.0, 0.0, 0.0),
    left=ClusterObject(2, 15.0, 3.0, 0.0),
    right=ClusterObject(3, 15.0, -3.0, 0.0),
    left_rear=ClusterObject(4, 10.0, 3.0, 0.0),
    right_rear=ClusterObject(5, 10.0, -3.0, 0.0),
  )
  assert filter_side_objects(slots, True) == slots
  filtered = filter_side_objects(slots, False)
  assert filtered.primary == slots.primary
  assert filtered.left is None
  assert filtered.right is None
  assert filtered.left_rear is None
  assert filtered.right_rear is None


def test_radar_backed_object_rejects_vision_only_and_invalid_values():
  assert radar_backed_object(lead(radar=False)) is None
  assert radar_backed_object(lead(track_id=-1)) is None
  assert radar_backed_object(lead(distance=float("nan"))) is None
  assert radar_backed_object(lead()).track_id == 1


@pytest.mark.parametrize(("forcing_stop", "stop_sign", "red_light", "should_stop"), [
  (True, False, False, False),
  (False, True, False, False),
  (False, False, True, True),
])
def test_stop_target_requires_a_committed_valid_stop(forcing_stop, stop_sign, red_light, should_stop):
  assert select_stop_target(True, True, True, forcing_stop, stop_sign, red_light, should_stop, 42.0) == 42.0
  assert select_stop_target(False, True, True, forcing_stop, stop_sign, red_light, should_stop, 42.0) is None
  assert select_stop_target(True, False, True, forcing_stop, stop_sign, red_light, should_stop, 42.0) is None
  assert select_stop_target(True, True, False, forcing_stop, stop_sign, red_light, should_stop, 42.0) is None


def test_stop_target_rejects_uncommitted_and_invalid_distance_and_clamps():
  assert select_stop_target(True, True, True, False, False, True, False, 42.0) is None
  assert select_stop_target(True, True, True, True, False, False, False, float("nan")) is None
  assert select_stop_target(True, True, True, True, False, False, False, -1.0) == pytest.approx(0.1)
  assert select_stop_target(True, True, True, True, False, False, False, 300.0) == pytest.approx(204.7)


def test_lane_change_direction_requires_active_committed_model_maneuver():
  assert select_lane_change_direction(True, True, True, "left") == "left"
  assert select_lane_change_direction(True, True, True, "right") == "right"
  assert select_lane_change_direction(False, True, True, "left") is None
  assert select_lane_change_direction(True, False, True, "left") is None
  assert select_lane_change_direction(True, True, False, "left") is None
  assert select_lane_change_direction(True, True, True, None) is None


@pytest.mark.parametrize("side", ["left", "right"])
def test_lane_change_animation_uses_route_backed_onset_then_live_phase(side):
  state = Ev9LaneChangeAnimationState()
  live_timestamp = 1_000_000_000
  assert update_lane_change_animation(state, True, None, False, live_timestamp, live_timestamp) is None
  phases = []
  for frame in range(15):
    command = update_lane_change_animation(
      state, True, side, False, live_timestamp, live_timestamp + frame * 10_000_000,
    )
    if command is not None:
      phases.append((frame, command.phase, command.counter_offset))
  assert phases == [
    (0, "trigger", 1), (4, "trigger", 1),
    (7, "steady", 2), (10, "steady", 2), (14, "steady", 2),
  ]

  # A received baseline frame queues one phase-locked steady override for the
  # following control tick rather than colliding on the receive tick.
  live_timestamp = 1_200_000_000
  assert update_lane_change_animation(state, True, side, False, live_timestamp, live_timestamp) is None
  command = update_lane_change_animation(
    state, True, side, False, live_timestamp, live_timestamp + 10_000_000,
  )
  assert command is not None
  assert (command.side, command.phase, command.counter_offset) == (side, "steady", 2)
  assert update_lane_change_animation(
    state, True, side, False, live_timestamp, live_timestamp + 20_000_000,
  ) is None


def test_canfd_speed_limit_state_reuses_camera_decode_and_preserves_unlimited():
  CP = SimpleNamespace(flags=int(HyundaiFlags.CANFD_LKA_STEERING))
  FPCP = SimpleNamespace(flags=int(HyundaiStarPilotFlags.SPEED_LIMIT_AVAILABLE))
  camera_values = {"ISLW_SpdCluMainDis": 60, "ISLA_SpdWrn": 1}
  cp = SimpleNamespace(vl={"FR_CMR_02_100ms": camera_values})
  cp_cam = SimpleNamespace(vl={"FR_CMR_02_100ms": {"ISLW_SpdCluMainDis": 35, "ISLA_SpdWrn": 0}})

  assert get_canfd_speed_limit_state(CP, FPCP, cp, cp_cam) == (60, True)
  CP.flags = 0
  assert get_canfd_speed_limit_state(CP, FPCP, cp, cp_cam) == (35, False)
  CP.flags = int(HyundaiFlags.CANFD_LKA_STEERING)
  camera_values["ISLW_SpdCluMainDis"] = 253
  camera_values["ISLA_SpdWrn"] = 0
  assert get_canfd_speed_limit_state(CP, FPCP, cp, cp_cam) == (253, False)
  for invalid in (0, 254, 255):
    camera_values["ISLW_SpdCluMainDis"] = invalid
    assert get_canfd_speed_limit_state(CP, FPCP, cp, cp_cam) == (0, False)

  FPCP.flags = 0
  camera_values["ISLW_SpdCluMainDis"] = 60
  assert get_canfd_speed_limit_state(CP, FPCP, cp, cp_cam) == (0, False)


def test_lane_change_animation_fails_neutral_for_physical_stalk_or_stale_sender():
  state = Ev9LaneChangeAnimationState()
  assert update_lane_change_animation(state, True, "right", True, 1_000_000_000, 1_000_000_000) is None
  assert update_lane_change_animation(state, True, "right", False, 1_000_000_000, 1_400_000_000) is None
  assert state.side is None


def test_lane_change_animation_sends_one_release_for_virtual_maneuver():
  state = Ev9LaneChangeAnimationState()
  timestamp = 1_000_000_000
  assert update_lane_change_animation(state, True, None, False, timestamp, timestamp) is None
  trigger = update_lane_change_animation(state, True, "right", False, timestamp, timestamp)
  assert trigger is not None and trigger.counter_offset == 1
  command = update_lane_change_animation(state, True, None, False, timestamp, timestamp + 10_000_000)
  assert command is not None
  assert (command.side, command.phase, command.counter_offset) == ("right", "release", 2)
  assert update_lane_change_animation(state, True, None, False, timestamp, timestamp + 20_000_000) is None


def test_lane_change_animation_does_not_collide_with_live_sender_during_onset():
  state = Ev9LaneChangeAnimationState()
  timestamp = 1_000_000_000
  assert update_lane_change_animation(state, True, None, False, timestamp, timestamp) is None
  command = update_lane_change_animation(state, True, "left", False, timestamp, timestamp)
  assert command is not None and command.phase == "trigger"
  for frame in range(1, 4):
    assert update_lane_change_animation(
      state, True, "left", False, timestamp, timestamp + frame * 10_000_000,
    ) is None
  timestamp = 1_040_000_000
  assert update_lane_change_animation(state, True, "left", False, timestamp, timestamp) is None


def test_lane_change_animation_direct_steady_and_early_release_counters():
  state = Ev9LaneChangeAnimationState()
  idle_timestamp = 1_000_000_000
  assert update_lane_change_animation(state, True, None, False, idle_timestamp, idle_timestamp) is None

  # Both scheduled trigger edges collide with newly received OEM baselines.
  first_timestamp = 1_010_000_000
  assert update_lane_change_animation(state, True, "left", False, first_timestamp, first_timestamp) is None
  for frame in range(1, 4):
    assert update_lane_change_animation(
      state, True, "left", False, first_timestamp, first_timestamp + frame * 10_000_000,
    ) is None
  second_timestamp = 1_050_000_000
  assert update_lane_change_animation(state, True, "left", False, second_timestamp, second_timestamp) is None
  for frame in range(5, 7):
    assert update_lane_change_animation(
      state, True, "left", False, second_timestamp, second_timestamp + (frame - 4) * 10_000_000,
    ) is None

  steady = update_lane_change_animation(state, True, "left", False, second_timestamp, 1_080_000_000)
  assert steady is not None
  assert (steady.phase, steady.counter_offset) == ("steady", 1)
  release = update_lane_change_animation(state, True, None, False, second_timestamp, 1_090_000_000)
  assert release is not None
  assert (release.phase, release.counter_offset) == ("release", 2)


def test_lane_change_animation_direction_swap_releases_before_new_trigger():
  state = Ev9LaneChangeAnimationState()
  timestamp = 1_000_000_000
  assert update_lane_change_animation(state, True, None, False, timestamp, timestamp) is None
  trigger = update_lane_change_animation(state, True, "left", False, timestamp, timestamp)
  assert trigger is not None and trigger.counter_offset == 1

  release = update_lane_change_animation(state, True, "right", False, timestamp, timestamp + 10_000_000)
  assert release is not None
  assert (release.side, release.phase, release.counter_offset) == ("left", "release", 2)

  # Do not restart from the same OEM baseline: that would move the semantic
  # counter backwards from release base+2 to a new trigger base+1.
  assert update_lane_change_animation(state, True, "right", False, timestamp, timestamp + 20_000_000) is None
  next_timestamp = timestamp + 200_000_000
  assert update_lane_change_animation(state, True, "right", False, next_timestamp, next_timestamp) is None
  for frame in range(1, 4):
    assert update_lane_change_animation(
      state, True, "right", False, next_timestamp, next_timestamp + frame * 10_000_000,
    ) is None
  trigger = update_lane_change_animation(state, True, "right", False, next_timestamp, next_timestamp + 40_000_000)
  assert trigger is not None
  assert (trigger.side, trigger.phase, trigger.counter_offset) == ("right", "trigger", 1)


def test_ev9_lane_change_payloads_are_route_captured_3c1_only():
  CP = CarParams.new_message()
  CP.carFingerprint = CAR.KIA_EV9
  CP.flags = int(HyundaiFlags.CANFD | HyundaiFlags.CANFD_LKA_STEERING)
  can_bus = CanBus(CP)
  packer = CANPacker(DBC[CP.carFingerprint][Bus.pt])
  parser = CANParser(DBC[CP.carFingerprint][Bus.pt], [("BLINKER_STALKS", 0)], can_bus.ECAN)

  def stock_values(payload, base_counter):
    parser.update([(1, [(0x3C1, bytes.fromhex(payload), can_bus.ECAN)])])
    values = dict(parser.vl["BLINKER_STALKS"])
    values["COUNTER_ALT"] = base_counter
    values["LEFT_BLINKER"] = 0
    values["RIGHT_BLINKER"] = 0
    return values

  left_stock = stock_values("25d0304010000000", 0xC)
  right_stock = stock_values("8630300041000000", 0x2)
  assert hyundaicanfd.create_ev9_cluster_lane_change_messages(packer, can_bus, left_stock, "left", "trigger", 1) == [
    (0x3C1, bytes.fromhex("25d0304010000000"), can_bus.ECAN),
  ]
  assert hyundaicanfd.create_ev9_cluster_lane_change_messages(packer, can_bus, left_stock, "left", "steady", 2) == [
    (0x3C1, bytes.fromhex("d6e0304000000000"), can_bus.ECAN),
  ]
  assert hyundaicanfd.create_ev9_cluster_lane_change_messages(packer, can_bus, left_stock, "left", "release", 3) == [
    (0x3C1, bytes.fromhex("5900300000000000"), can_bus.ECAN),
  ]
  assert hyundaicanfd.create_ev9_cluster_lane_change_messages(packer, can_bus, right_stock, "right", "trigger", 1) == [
    (0x3C1, bytes.fromhex("8630300041000000"), can_bus.ECAN),
  ]
  assert hyundaicanfd.create_ev9_cluster_lane_change_messages(packer, can_bus, right_stock, "right", "steady", 2) == [
    (0x3C1, bytes.fromhex("1a40300001000000"), can_bus.ECAN),
  ]
  assert hyundaicanfd.create_ev9_cluster_lane_change_messages(packer, can_bus, right_stock, "right", "release", 3) == [
    (0x3C1, bytes.fromhex("3e50300000000000"), can_bus.ECAN),
  ]
  assert hyundaicanfd.create_ev9_cluster_lane_change_messages(packer, can_bus, right_stock, "none", "steady", 2) == []
  assert hyundaicanfd.create_ev9_cluster_lane_change_messages(packer, can_bus, right_stock, "left", None, 2) == []
  assert hyundaicanfd.create_ev9_cluster_lane_change_messages(packer, can_bus, {}, "left", "steady", 2) == []
  assert hyundaicanfd.create_ev9_cluster_lane_change_messages(packer, can_bus, right_stock, "left", "steady") == []
  assert hyundaicanfd.create_ev9_cluster_lane_change_messages(packer, can_bus, right_stock, "left", "steady", 4) == []
  assert hyundaicanfd.create_ev9_cluster_lane_change_messages(
    packer, can_bus, right_stock | {"COUNTER_ALT": 0xF}, "left", "steady", 2,
  ) == []


def test_route_derived_mrr35_display_qualifiers():
  base = {"NEW_SIGNAL_7": 201, "NEW_SIGNAL_3": 2, "NEW_SIGNAL_12": 10,
          "NEW_SIGNAL_15": 2, "NEW_SIGNAL_17": 1}
  assert ev9_dash_display_candidate(base)
  assert not ev9_dash_side_candidate(base)
  assert not ev9_dash_side_retention_candidate(base)

  strict = base | {"NEW_SIGNAL_7": 281}
  assert ev9_dash_display_candidate(strict)
  assert ev9_dash_side_candidate(strict)
  assert ev9_dash_side_retention_candidate(strict)

  for signal in ("NEW_SIGNAL_3", "NEW_SIGNAL_12", "NEW_SIGNAL_15", "NEW_SIGNAL_17"):
    assert not ev9_dash_side_candidate(strict | {signal: 0})


def test_ccnc_status_encodes_stable_stock_object_slots():
  CP = CarParams.new_message()
  CP.carFingerprint = CAR.KIA_EV9
  CP.flags = int(HyundaiFlags.CANFD | HyundaiFlags.CCNC | HyundaiFlags.CANFD_ANGLE_STEERING |
                 HyundaiFlags.CANFD_LKA_STEERING | HyundaiFlags.CANFD_LKA_STEERING_ALT)
  packer = CANPacker(DBC[CP.carFingerprint][Bus.pt])
  can_bus = CanBus(CP)
  parser = CANParser(DBC[CP.carFingerprint][Bus.pt], [("CCNC_0x161", 0), ("CCNC_0x162", 0)], can_bus.ECAN)
  scene = Ev9DashScene(objects=ClusterObjectSlots(
    primary=ClusterObject(1, 30.0, 0.2, -1.0),
    left=ClusterObject(2, 40.0, 3.4, 0.0),
    right=ClusterObject(3, 35.0, -3.1, 0.0),
    left_rear=ClusterObject(4, 18.0, 3.0, 0.0),
    right_rear=ClusterObject(5, 22.0, -3.0, 0.0),
  ), speed_limit_raw=60, speed_limit_warning=True)
  out = SimpleNamespace(vCruiseCluster=100.0, vEgo=10.0)
  hud = SimpleNamespace(leadDistanceBars=3)

  parser.update([(1, hyundaicanfd.create_ccnc_angle_long_status_messages(
    packer, CP, can_bus, 1, enabled=True, main_cruise_enabled=True,
    hud=hud, out=out, dash_scene=scene,
  ))])
  status = parser.vl["CCNC_0x162"]
  assert parser.vl["CCNC_0x161"]["TARGET_DISTANCE"] == pytest.approx(16.3)
  assert parser.vl["CCNC_0x161"]["DISTANCE_LEAD"] == 2
  assert status["LEAD"] == 2
  assert status["LEAD_DISTANCE"] == pytest.approx(29.8)
  assert status["LEAD_LATERAL"] == 0.0
  assert status["LEAD_ALT"] == 0
  assert status["LEAD_LEFT"] == 1
  assert status["LEAD_LEFT_DISTANCE"] == pytest.approx(39.8)
  assert status["LEAD_LEFT_LATERAL"] == pytest.approx(3.0)
  assert status["LEAD_RIGHT"] == 1
  assert status["LEAD_RIGHT_DISTANCE"] == pytest.approx(34.8)
  assert status["LEAD_RIGHT_LATERAL"] == pytest.approx(3.0)
  assert status["LEAD_LEFT_REAR_STATUS"] == 1
  assert status["LEAD_LEFT_REAR_DISTANCE"] == pytest.approx(17.8)
  assert status["LEAD_RIGHT_REAR_STATUS"] == 1
  assert status["LEAD_RIGHT_REAR_DISTANCE"] == pytest.approx(21.8)
  assert status["COUNTRY"] == 7
  assert status["SPEEDLIMIT"] == 60
  assert status["SPEEDLIMIT_FLASH"] == 4
  assert status["SPEEDLIMIT_WEATHER"] == 0

  parser.update([(2, hyundaicanfd.create_ccnc_angle_long_status_messages(
    packer, CP, can_bus, 2, enabled=True, main_cruise_enabled=True,
    hud=hud, out=out, dash_scene=Ev9DashScene(stop_target_distance=42.0, speed_limit_raw=253),
  ))])
  assert parser.vl["CCNC_0x161"]["TARGET_DISTANCE"] == pytest.approx(42.0)
  assert parser.vl["CCNC_0x162"]["SPEEDLIMIT"] == 253
  assert parser.vl["CCNC_0x162"]["SPEEDLIMIT_FLASH"] == 2


def test_ccnc_side_kill_switch_suppresses_all_radar_side_slots():
  CP = CarParams.new_message()
  CP.carFingerprint = CAR.KIA_EV9
  CP.flags = int(HyundaiFlags.CANFD | HyundaiFlags.CCNC | HyundaiFlags.CANFD_LKA_STEERING)
  packer = CANPacker(DBC[CP.carFingerprint][Bus.pt])
  can_bus = CanBus(CP)
  parser = CANParser(DBC[CP.carFingerprint][Bus.pt], [("CCNC_0x162", 0)], can_bus.ECAN)
  scene = Ev9DashScene(
    objects=ClusterObjectSlots(
      left=ClusterObject(2, 20.0, 3.0, 0.0),
      left_rear=ClusterObject(3, 10.0, 3.0, 0.0),
    ),
    side_objects_enabled=False,
  )
  parser.update([(1, hyundaicanfd.create_ccnc_angle_long_status_messages(
    packer, CP, can_bus, 1, enabled=True,
    hud=SimpleNamespace(leadDistanceBars=3), out=SimpleNamespace(vCruiseCluster=100.0, vEgo=10.0),
    dash_scene=scene,
  ))])
  status = parser.vl["CCNC_0x162"]
  assert status["LEAD_LEFT"] == 0
  assert status["LEAD_LEFT_REAR_STATUS"] == 0


def test_ccnc_status_hides_objects_outside_active_hda():
  CP = CarParams.new_message()
  CP.carFingerprint = CAR.KIA_EV9
  CP.flags = int(HyundaiFlags.CANFD | HyundaiFlags.CCNC | HyundaiFlags.CANFD_ANGLE_STEERING |
                 HyundaiFlags.CANFD_LKA_STEERING | HyundaiFlags.CANFD_LKA_STEERING_ALT)
  packer = CANPacker(DBC[CP.carFingerprint][Bus.pt])
  can_bus = CanBus(CP)
  parser = CANParser(DBC[CP.carFingerprint][Bus.pt], [("CCNC_0x161", 0), ("CCNC_0x162", 0)], can_bus.ECAN)
  scene = Ev9DashScene(objects=ClusterObjectSlots(primary=ClusterObject(1, 30.0, 0.0, 0.0)))

  parser.update([(1, hyundaicanfd.create_ccnc_angle_long_status_messages(
    packer, CP, can_bus, 1, enabled=False, main_cruise_enabled=True,
    hud=SimpleNamespace(leadDistanceBars=3), out=SimpleNamespace(vCruiseCluster=100.0, vEgo=10.0),
    dash_scene=scene,
  ))])
  assert parser.vl["CCNC_0x161"]["DISTANCE_LEAD"] == 0
  assert parser.vl["CCNC_0x162"]["LEAD"] == 0
