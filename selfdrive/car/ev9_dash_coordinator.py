#!/usr/bin/env python3

from cereal import car, log
from opendbc.car import structs
from opendbc.car.hyundai.ev9_dash import (
  ClusterObjectSlots,
  Ev9DashObjectTracker,
  Ev9DashScene,
  Ev9LaneBoundary,
  Ev9DashTrackCandidates,
  Ev9LaneOutlineTracker,
  Ev9RawBlindspotGateState,
  Ev9TargetLineTracker,
  display_context_valid,
  resolve_ev9_blindspot_state,
  select_lane_change_direction,
  select_ev9_lane_boundaries,
  update_ev9_raw_blindspot_gate,
  validate_slots_for_output,
)


class EV9DashCoordinator:
  """EV9-only cluster and blind-spot reconstruction orchestration."""

  @staticmethod
  def initialize(card_process) -> None:
    card_process.ev9_dash_tracker = Ev9DashObjectTracker()
    card_process.ev9_lane_outline_tracker = Ev9LaneOutlineTracker()
    card_process.ev9_target_line_tracker = Ev9TargetLineTracker()
    card_process.ev9_raw_blindspot_gate = Ev9RawBlindspotGateState()
    card_process.ev9_dash_slots = ClusterObjectSlots()
    card_process.ev9_dash_scene = Ev9DashScene()
    EV9DashCoordinator.refresh_settings(card_process)

  @staticmethod
  def refresh_settings(card_process) -> None:
    card_process.ev9_bsm_reconstruction_enabled = card_process.params.get_bool("KiaEv9ClusterSideObjectsEnabled")
    card_process.ev9_dash_headway_enabled = card_process.params.get_bool("KiaEv9ClusterHeadwayEnabled")
    card_process.ev9_dash_objects_enabled = card_process.params.get_bool("KiaEv9ClusterObjectsEnabled")

  def _update_ev9_dash_tracker(self, CS: car.CarState, RD: structs.RadarDataT | None) -> None:
    if str(self.CP.carFingerprint) != "KIA_EV9":
      return

    radar_valid = self.sm.seen['radarState'] and self.sm.alive['radarState'] and self.sm.valid['radarState']
    context_valid = display_context_valid(
      radar_valid,
      bool(CS.cruiseState.available),
      CS.gearShifter == structs.CarState.GearShifter.drive,
    )
    if not context_valid:
      self.ev9_dash_slots = self.ev9_dash_tracker.clear()
      return
    if RD is None:
      return
    if any(RD.errors.to_dict().values()):
      self.ev9_dash_slots = self.ev9_dash_tracker.clear()
      return

    fused_lead = self.sm['radarState'].leadOne
    preferred_primary_track_id = int(fused_lead.radarTrackId) \
      if fused_lead.status and fused_lead.radar else -1
    preferred_primary_model_prob = float(fused_lead.modelProb) \
      if fused_lead.status and fused_lead.radar else 0.0
    candidates = getattr(self.RI, "ev9_dash_track_candidates", Ev9DashTrackCandidates())
    self.ev9_dash_slots = self.ev9_dash_tracker.update(
      list(RD.points),
      preferred_primary_track_id,
      preferred_primary_model_prob,
      set(candidates.display),
      set(candidates.side),
      set(candidates.side_retention),
      float(CS.vEgo),
      bool(CS.standstill),
    )

  def _update_ev9_raw_blindspot_gate(self, CS: car.CarState, RD: structs.RadarDataT | None) -> None:
    if str(self.CP.carFingerprint) != "KIA_EV9":
      return

    native_left = bool(CS.leftBlindspot)
    native_right = bool(CS.rightBlindspot)
    native_fresh = bool(getattr(self.CI.CS, "native_blindspot_fresh", False))

    if not self.ev9_bsm_reconstruction_enabled or RD is None or any(RD.errors.to_dict().values()):
      self.ev9_raw_blindspot_gate.clear()
      self.CI.CS.ev9_reconstructed_left_blindspot = False
      self.CI.CS.ev9_reconstructed_right_blindspot = False
      self.CI.CS.ev9_reconstructed_blindspot_ts = 0
      CS.leftBlindspot, CS.rightBlindspot = resolve_ev9_blindspot_state(
        native_left, native_right, native_fresh, False, False, self.ev9_bsm_reconstruction_enabled,
      )
      return

    candidates = getattr(self.RI, "ev9_dash_track_candidates", Ev9DashTrackCandidates())
    left, right = update_ev9_raw_blindspot_gate(
      self.ev9_raw_blindspot_gate,
      int(getattr(self.CI.CS, "ev9_raw_blindspot_state", 0)),
      bool(getattr(self.CI.CS, "ev9_raw_blindspot_fresh", False)),
      CS.gearShifter == structs.CarState.GearShifter.drive,
      list(RD.points),
      set(candidates.display),
      set(candidates.side),
      float(CS.vEgo),
    )
    self.CI.CS.ev9_reconstructed_left_blindspot = left
    self.CI.CS.ev9_reconstructed_right_blindspot = right
    self.CI.CS.ev9_reconstructed_blindspot_ts = int(getattr(self.CI.CS, "ev9_raw_blindspot_ts", 0)) \
      if bool(getattr(self.CI.CS, "ev9_raw_blindspot_fresh", False)) else 0
    # Publish the same qualified fallback to openpilot. This restores normal
    # lane-change blocking and blindspot alerts after ADAS suppression removes
    # the native 0x1BA source; the controller also uses it for reconstructed
    # mirror lamps and warning escalation.
    CS.leftBlindspot, CS.rightBlindspot = resolve_ev9_blindspot_state(
      native_left, native_right, native_fresh, left, right, self.ev9_bsm_reconstruction_enabled,
    )

  def _update_ev9_dash_scene(self, CS: car.CarState, CC: car.CarControl) -> None:
    if str(self.CP.carFingerprint) != "KIA_EV9":
      return

    radar_valid = self.sm.seen['radarState'] and self.sm.alive['radarState'] and self.sm.valid['radarState']
    objects = validate_slots_for_output(
      self.ev9_dash_slots,
      self.sm['radarState'].leadOne,
      float(CS.vEgo),
      bool(CS.standstill),
    ) if radar_valid else ClusterObjectSlots()
    if not self.ev9_dash_objects_enabled:
      objects = ClusterObjectSlots()

    model_valid = self.sm.seen['modelV2'] and self.sm.alive['modelV2'] and self.sm.valid['modelV2']
    model = self.sm['modelV2']
    display_active = bool(CS.cruiseState.available and CS.gearShifter == structs.CarState.GearShifter.drive)

    # Match the stock EV9 headway marker: it represents the speed-based desired
    # following distance. The planner-derived stop/follow target switched
    # sources as leads were acquired and dropped, making the cluster line feel
    # unintuitive and visibly jittery even though it was display-only.
    target_line_inputs_updated = bool(self.sm.updated['longitudinalPlan'])
    target_line_distance = self.ev9_target_line_tracker.update(
      display_active and self.ev9_dash_headway_enabled,
      target_line_inputs_updated,
      None,
      1.626 * max(float(CS.vEgo), 0.0),
    )

    model_meta = model.meta
    lane_change_committed = model_meta.laneChangeState in (
      log.LaneChangeState.laneChangeStarting,
      log.LaneChangeState.laneChangeFinishing,
    )
    direction = None
    if model_meta.laneChangeDirection == log.LaneChangeDirection.left:
      direction = "left"
    elif model_meta.laneChangeDirection == log.LaneChangeDirection.right:
      direction = "right"
    lane_change_direction = select_lane_change_direction(
      bool(CC.latActive), model_valid, lane_change_committed, direction,
    )

    model_updated = bool(self.sm.updated['modelV2'])
    left_boundary, right_boundary = select_ev9_lane_boundaries(
      list(model.laneLineProbs), list(model.laneLines), list(model.roadEdgeStds), list(model.roadEdges),
    ) if model_valid and model_updated else (Ev9LaneBoundary(), Ev9LaneBoundary())
    lane_outline = self.ev9_lane_outline_tracker.update(
      display_active,
      model_valid,
      model_updated,
      left_boundary,
      right_boundary,
      float(model.action.desiredCurvature),
    )

    self.ev9_dash_scene = Ev9DashScene(
      objects=objects,
      lane_outline=lane_outline,
      target_line_distance=target_line_distance,
      lane_change_direction=lane_change_direction,
      speed_limit_raw=int(getattr(self.CI.CS, "dashboard_speed_limit_raw", 0)),
      speed_limit_warning=bool(getattr(self.CI.CS, "dashboard_speed_limit_warning", False)),
      objects_enabled=self.ev9_dash_objects_enabled,
      headway_enabled=self.ev9_dash_headway_enabled,
    )
    self.CI.CS.ev9_dash_scene = self.ev9_dash_scene
