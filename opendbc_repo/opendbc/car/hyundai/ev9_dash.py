from dataclasses import dataclass
import math
from typing import Any


MIN_OBJECT_DISTANCE = 0.1
MAX_TARGET_DISTANCE = 204.7
SIDE_MOVING_OBJECT_MIN_SPEED = 2.78
EV9_LANE_CHANGE_LIVE_STALE_NS = 300_000_000
EV9_LANE_CHANGE_ONSET_PHASES = {
  0: "trigger",
  4: "trigger",
  7: "steady",
  10: "steady",
  14: "steady",
}


def display_context_valid(radar_valid: bool, main_enabled: bool, drive_gear: bool) -> bool:
  """Keep the display-only tracker inside the stock Drive/Main envelope."""
  return bool(radar_valid and main_enabled and drive_gear)


def select_stop_target(long_active: bool, starpilot_plan_valid: bool, longitudinal_plan_valid: bool,
                       forcing_stop: bool, stop_sign_confirmed: bool, red_light: bool,
                       should_stop: bool, distance: float) -> float | None:
  """Select a committed planner stop for the display-only CCNC target line."""
  stop_scene = forcing_stop or stop_sign_confirmed or (red_light and should_stop)
  if not (long_active and starpilot_plan_valid and longitudinal_plan_valid and stop_scene):
    return None
  if not math.isfinite(distance):
    return None
  return min(max(float(distance), MIN_OBJECT_DISTANCE), MAX_TARGET_DISTANCE)


def select_lane_change_direction(lat_active: bool, model_valid: bool,
                                 lane_change_committed: bool, direction: str | None) -> str | None:
  """Expose only an active comma lane-change maneuver, never pre-lane-change intent."""
  return direction if lat_active and model_valid and lane_change_committed and direction in ("left", "right") else None


@dataclass(frozen=True)
class ClusterObject:
  track_id: int
  distance: float
  lateral: float
  relative_speed: float
  motion_confirmed: bool = False
  raw_relative_speed: float | None = None


@dataclass(frozen=True)
class ClusterObjectSlots:
  primary: ClusterObject | None = None
  left: ClusterObject | None = None
  right: ClusterObject | None = None
  left_rear: ClusterObject | None = None
  right_rear: ClusterObject | None = None


@dataclass(frozen=True)
class Ev9DashTrackCandidates:
  display: frozenset[int] = frozenset()
  side: frozenset[int] = frozenset()
  side_retention: frozenset[int] = frozenset()


@dataclass(frozen=True)
class Ev9DashScene:
  objects: ClusterObjectSlots = ClusterObjectSlots()
  stop_target_distance: float | None = None
  lane_change_direction: str | None = None
  speed_limit_raw: int = 0
  speed_limit_warning: bool = False
  side_objects_enabled: bool = True


def filter_side_objects(slots: ClusterObjectSlots, side_objects_enabled: bool) -> ClusterObjectSlots:
  """Apply the persistent side-object kill switch without changing primary output."""
  return slots if side_objects_enabled else ClusterObjectSlots(primary=slots.primary)


@dataclass
class Ev9LaneChangeAnimationState:
  side: str | None = None
  frame: int = 0
  live_timestamp_nanos: int = 0
  live_frame_pending: bool = False
  semantic_phase: str | None = None
  counter_offset: int = 0
  restart_after_timestamp_nanos: int = 0


@dataclass(frozen=True)
class Ev9LaneChangeAnimationCommand:
  side: str
  phase: str
  counter_offset: int


def update_lane_change_animation(state: Ev9LaneChangeAnimationState, enabled: bool,
                                 direction: str | None, physical_stalk_active: bool,
                                 live_timestamp_nanos: int, now_nanos: int) -> Ev9LaneChangeAnimationCommand | None:
  """Return an EV9 0x3C1 payload phase for a virtual comma maneuver.

  The OEM sender remains live after ADAS suppression. Synthetic edge frames are
  sent only for a virtual maneuver; steady overrides follow one control tick
  after each received baseline frame instead of starting a competing 5 Hz timer.
  """
  timestamp = int(live_timestamp_nanos)
  age_nanos = int(now_nanos) - timestamp
  live_fresh = timestamp > 0 and 0 <= age_nanos <= EV9_LANE_CHANGE_LIVE_STALE_NS
  if state.restart_after_timestamp_nanos > 0 and timestamp > state.restart_after_timestamp_nanos:
    state.restart_after_timestamp_nanos = 0
  restart_ready = state.restart_after_timestamp_nanos == 0
  active = enabled and direction in ("left", "right") and not physical_stalk_active and live_fresh and restart_ready

  if not active:
    previous_side = state.side
    release_offset = state.counter_offset + 1 if state.semantic_phase is not None else 0
    release = bool(previous_side is not None and release_offset > 0 and
                   enabled and live_fresh and not physical_stalk_active)
    if release:
      state.restart_after_timestamp_nanos = timestamp
    state.side = None
    state.frame = 0
    state.live_timestamp_nanos = timestamp
    state.live_frame_pending = False
    state.semantic_phase = None
    state.counter_offset = 0
    return Ev9LaneChangeAnimationCommand(previous_side, "release", release_offset) if release else None

  if state.side is not None and direction != state.side:
    previous_side = state.side
    release_offset = state.counter_offset + 1 if state.semantic_phase is not None else 0
    if release_offset > 0:
      state.restart_after_timestamp_nanos = timestamp
    state.side = None
    state.frame = 0
    state.live_timestamp_nanos = timestamp
    state.live_frame_pending = False
    state.semantic_phase = None
    state.counter_offset = 0
    return Ev9LaneChangeAnimationCommand(previous_side, "release", release_offset) \
      if release_offset > 0 else None

  if direction != state.side:
    state.side = direction
    state.frame = 0
    state.live_frame_pending = False
    state.semantic_phase = None
    state.counter_offset = 0

  new_live_frame = timestamp != state.live_timestamp_nanos
  phase = EV9_LANE_CHANGE_ONSET_PHASES.get(state.frame)
  if new_live_frame and phase is not None:
    # Never arbitrate a synthetic edge against a baseline frame received on
    # this control cycle. Stock captures vary in edge count, so dropping this
    # one sample is safer than emitting duplicate traffic for the same CAN ID.
    phase = None
  elif state.frame > max(EV9_LANE_CHANGE_ONSET_PHASES):
    if new_live_frame:
      state.live_frame_pending = True
    elif state.live_frame_pending:
      phase = "steady"
      state.live_frame_pending = False

  state.live_timestamp_nanos = timestamp
  state.frame += 1
  if phase is None or state.side is None:
    return None

  # Repeated trigger/steady frames retain the same semantic counter. If every
  # trigger collided with a live OEM frame, the first steady frame is a direct
  # steady onset and advances only once, matching stock captures.
  if phase != state.semantic_phase:
    state.semantic_phase = phase
    state.counter_offset += 1
  return Ev9LaneChangeAnimationCommand(state.side, phase, state.counter_offset)


@dataclass
class _TrackedObject:
  track_id: int
  distance: float
  lateral: float
  relative_speed: float
  hits: int = 1
  misses: int = 0
  confirmed: bool = False
  raw_distance: float = 0.0
  raw_lateral: float = 0.0
  raw_relative_speed: float = 0.0
  right_entry_hits: int = 0
  side_motion_hits: int = 0
  side_motion_confirmed: bool = False
  side_qualified: bool = False
  side_retention_qualified: bool = False
  primary_confidence_hits: int = 0
  primary_confidence_misses: int = 0
  primary_confident: bool = False


class Ev9DashObjectTracker:
  """Route-qualified display tracker that never participates in planning or control."""

  # MRR35 publishes at 20 Hz. Three samples prevent one-frame acquisitions,
  # while a four-sample dropout hold removes brief radar channel churn.
  ACQUISITION_SAMPLES = 3
  DROPOUT_HOLD_SAMPLES = 4
  EMA_ALPHA = 0.35
  PRIMARY_ENTRY_HALF_WIDTH = 2.5
  PRIMARY_RETENTION_HALF_WIDTH = 2.5
  # The held-out route's multi-second false display leads had materially lower
  # fused model confidence. Apply this only to the display path; radarState and
  # planning remain untouched. Distant leads use the stricter route-backed bar.
  PRIMARY_MODEL_PROB_NEAR = 0.75
  PRIMARY_MODEL_PROB_FAR = 0.90
  PRIMARY_MODEL_PROB_FAR_DISTANCE = 100.0
  SIDE_INNER_WIDTH = 1.8
  SIDE_RETENTION_INNER_WIDTH = 0.25
  MAX_DISTANCE = 180.0

  def __init__(self) -> None:
    self.tracks: dict[int, _TrackedObject] = {}
    self.slot_track_ids: dict[str, int] = {
      "primary": -1,
      "left": -1,
      "right": -1,
    }

  def clear(self) -> ClusterObjectSlots:
    self.tracks.clear()
    for slot in self.slot_track_ids:
      self.slot_track_ids[slot] = -1
    return ClusterObjectSlots()

  @staticmethod
  def _valid_point(point: Any) -> bool:
    values = (float(getattr(point, "dRel", 0.0)), float(getattr(point, "yRel", 0.0)),
              float(getattr(point, "vRel", 0.0)))
    return bool(getattr(point, "measured", False)) and int(getattr(point, "trackId", -1)) >= 0 and \
      MIN_OBJECT_DISTANCE < values[0] <= Ev9DashObjectTracker.MAX_DISTANCE and all(math.isfinite(v) for v in values)

  @staticmethod
  def _as_cluster_object(track: _TrackedObject) -> ClusterObject:
    return ClusterObject(track.track_id, track.distance, track.lateral, track.relative_speed,
                         track.side_motion_confirmed, track.raw_relative_speed)

  @staticmethod
  def _side_motion_valid(track: _TrackedObject, v_ego: float, standstill: bool) -> bool:
    # A stationary wall has vRel ~= -vEgo. At a stop, retain only a target
    # whose motion was previously established or which is moving now.
    if standstill or abs(float(v_ego)) <= 0.1:
      return track.side_motion_confirmed or abs(track.raw_relative_speed) >= SIDE_MOVING_OBJECT_MIN_SPEED
    return abs(float(v_ego) + track.raw_relative_speed) >= SIDE_MOVING_OBJECT_MIN_SPEED

  @classmethod
  def _primary_confidence_sample(cls, distance: float, model_prob: float) -> bool:
    threshold = cls.PRIMARY_MODEL_PROB_FAR \
      if distance > cls.PRIMARY_MODEL_PROB_FAR_DISTANCE else cls.PRIMARY_MODEL_PROB_NEAR
    return math.isfinite(model_prob) and model_prob >= threshold

  def update(self, points: list[Any], preferred_primary_track_id: int,
             preferred_primary_model_prob: float,
             qualified_track_ids: set[int], side_qualified_track_ids: set[int],
             side_retention_track_ids: set[int], v_ego: float, standstill: bool) -> ClusterObjectSlots:
    # A present track that no longer passes the route-derived display discriminator is
    # removed immediately. A genuinely absent track receives the short hold.
    incoming_track_ids = {int(point.trackId) for point in points if self._valid_point(point)}
    # The raw display discriminator is strong on the preserved d4-d6
    # firmware, but a held-out stock route uses a different score range. The
    # fused radar-backed lead is already the strongest available primary proof,
    # so never make that route-variant score a mandatory primary gate.
    allowed_track_ids = set(qualified_track_ids)
    if preferred_primary_track_id in incoming_track_ids:
      allowed_track_ids.add(preferred_primary_track_id)
    for track_id in set(self.tracks) & (incoming_track_ids - allowed_track_ids):
      del self.tracks[track_id]
    for slot, track_id in self.slot_track_ids.items():
      if track_id in incoming_track_ids and track_id not in allowed_track_ids:
        self.slot_track_ids[slot] = -1

    seen: set[int] = set()
    for point in points:
      if not self._valid_point(point):
        continue

      track_id = int(point.trackId)
      if track_id not in allowed_track_ids:
        continue
      seen.add(track_id)
      distance = float(point.dRel)
      lateral = float(point.yRel)
      relative_speed = float(point.vRel)
      track = self.tracks.get(track_id)
      if track is None:
        side_motion = abs(relative_speed) >= SIDE_MOVING_OBJECT_MIN_SPEED if standstill or abs(float(v_ego)) <= 0.1 else \
          abs(float(v_ego) + relative_speed) >= SIDE_MOVING_OBJECT_MIN_SPEED
        primary_confidence = track_id == preferred_primary_track_id and \
          self._primary_confidence_sample(distance, preferred_primary_model_prob)
        self.tracks[track_id] = _TrackedObject(
          track_id, distance, lateral, relative_speed,
          raw_distance=distance, raw_lateral=lateral, raw_relative_speed=relative_speed,
          side_motion_hits=int(side_motion),
          side_qualified=track_id in side_qualified_track_ids,
          side_retention_qualified=track_id in side_retention_track_ids,
          primary_confidence_hits=int(primary_confidence),
        )
        continue

      track.raw_distance = distance
      track.raw_lateral = lateral
      track.raw_relative_speed = relative_speed
      track.side_qualified = track_id in side_qualified_track_ids
      track.side_retention_qualified = track_id in side_retention_track_ids
      track.distance += self.EMA_ALPHA * (distance - track.distance)
      track.lateral += self.EMA_ALPHA * (lateral - track.lateral)
      track.relative_speed += self.EMA_ALPHA * (relative_speed - track.relative_speed)
      track.hits += 1
      track.misses = 0
      track.confirmed = track.confirmed or track.hits >= self.ACQUISITION_SAMPLES

      side_motion = abs(track.raw_relative_speed) >= SIDE_MOVING_OBJECT_MIN_SPEED \
        if standstill or abs(float(v_ego)) <= 0.1 else \
        abs(float(v_ego) + track.raw_relative_speed) >= SIDE_MOVING_OBJECT_MIN_SPEED
      track.side_motion_hits = track.side_motion_hits + 1 if side_motion else 0
      track.side_motion_confirmed = track.side_motion_confirmed or \
        track.side_motion_hits >= self.ACQUISITION_SAMPLES

      if track_id == preferred_primary_track_id:
        primary_confidence = self._primary_confidence_sample(distance, preferred_primary_model_prob)
        if primary_confidence:
          track.primary_confidence_hits += 1
          track.primary_confidence_misses = 0
          track.primary_confident = track.primary_confident or \
            track.primary_confidence_hits >= self.ACQUISITION_SAMPLES
        else:
          track.primary_confidence_hits = 0
          track.primary_confidence_misses += 1
          if track.primary_confidence_misses > self.DROPOUT_HOLD_SAMPLES:
            track.primary_confident = False
      else:
        track.primary_confidence_hits = 0
        track.primary_confidence_misses = 0
        track.primary_confident = False

    for track_id, track in list(self.tracks.items()):
      if track_id not in seen:
        track.hits = 0
        track.misses += 1
        if track.misses > self.DROPOUT_HOLD_SAMPLES:
          del self.tracks[track_id]

    # The stock right slot has an asymmetric entry/retention envelope. This
    # rejects long-lived d6 roadside ghosts without keying on radar addresses.
    for track in self.tracks.values():
      right_entry = track.track_id in seen and track.side_qualified and \
        -4.3 < track.raw_lateral < -2.2 and track.raw_distance < 60.0 and \
        self._side_motion_valid(track, v_ego, standstill)
      track.right_entry_hits = track.right_entry_hits + 1 if right_entry else 0

    confirmed = [track for track in self.tracks.values() if track.confirmed]
    previous_primary_track_id = self.slot_track_ids["primary"]
    current_left_track_id = self.slot_track_ids["left"]
    current_right_track_id = self.slot_track_ids["right"]

    def left_candidate(track: _TrackedObject) -> bool:
      entered = track.side_qualified and self.SIDE_INNER_WIDTH < track.lateral < 4.0 and \
        track.distance < 80.0 and self._side_motion_valid(track, v_ego, standstill)
      promoting = track.track_id == current_left_track_id == preferred_primary_track_id and \
        track.side_retention_qualified and \
        self.SIDE_RETENTION_INNER_WIDTH < track.raw_lateral < 4.5 and track.raw_distance < 82.0 and \
        self._side_motion_valid(track, v_ego, standstill)
      handoff = track.track_id == previous_primary_track_id and track.side_retention_qualified and \
        self.SIDE_INNER_WIDTH < track.raw_lateral < 4.5 and track.raw_distance < 82.0 and \
        self._side_motion_valid(track, v_ego, standstill)
      return entered or promoting or handoff

    left = sorted((track for track in confirmed if left_candidate(track)), key=lambda track: track.distance)

    def right_candidate(track: _TrackedObject) -> bool:
      retained = track.track_id == current_right_track_id and track.side_retention_qualified and \
        -4.5 < track.raw_lateral < -1.5 and track.raw_distance < 62.0 and \
        self._side_motion_valid(track, v_ego, standstill)
      promoting = track.track_id == current_right_track_id == preferred_primary_track_id and \
        track.side_retention_qualified and \
        -4.5 < track.raw_lateral < -self.SIDE_RETENTION_INNER_WIDTH and track.raw_distance < 62.0 and \
        self._side_motion_valid(track, v_ego, standstill)
      handoff = track.track_id == previous_primary_track_id and track.side_retention_qualified and \
        -4.5 < track.raw_lateral < -2.2 and track.raw_distance < 62.0 and \
        self._side_motion_valid(track, v_ego, standstill)
      entered = track.right_entry_hits >= self.ACQUISITION_SAMPLES and \
        self._side_motion_valid(track, v_ego, standstill)
      return retained or promoting or handoff or entered

    right = sorted((track for track in confirmed if right_candidate(track)), key=lambda track: track.distance)

    # The primary white box must remain the radar-backed fused lead. Raw
    # nearest-track selection created convincing but false center objects.
    current_primary_track_id = previous_primary_track_id
    primary = next((track for track in confirmed if track.track_id == preferred_primary_track_id and
                    track.primary_confident), None) \
      if preferred_primary_track_id >= 0 else None
    if primary is not None:
      primary_half_width = self.PRIMARY_RETENTION_HALF_WIDTH \
        if primary.track_id == current_primary_track_id else self.PRIMARY_ENTRY_HALF_WIDTH
      if abs(primary.raw_lateral) > primary_half_width:
        primary = None
    self.slot_track_ids["primary"] = primary.track_id if primary is not None else -1
    if primary is not None:
      left = [track for track in left if track.track_id != primary.track_id]
      right = [track for track in right if track.track_id != primary.track_id]

    def choose_side(slot: str, candidates: list[_TrackedObject]) -> _TrackedObject | None:
      chosen = next((track for track in candidates if track.track_id == self.slot_track_ids[slot]), None)
      # Stock moves one physical identity atomically between the center and
      # adjacent slot. Prioritize that handoff even when another side target is
      # present, otherwise a lane-crossing car can disappear for one scan.
      handoff = next((track for track in candidates if track.track_id == previous_primary_track_id), None)
      if primary is None and handoff is not None:
        chosen = handoff
      # Outside a known center-to-side handoff, acquire only an unambiguous
      # scene. The preserved routes do not expose a trustworthy retained source
      # for the separate fixed rear marker.
      if chosen is None and len(candidates) == 1:
        chosen = candidates[0]
      self.slot_track_ids[slot] = chosen.track_id if chosen is not None else -1
      return chosen

    left_object = choose_side("left", left)
    right_object = choose_side("right", right)
    return ClusterObjectSlots(
      primary=self._as_cluster_object(primary) if primary is not None else None,
      left=self._as_cluster_object(left_object) if left_object is not None else None,
      right=self._as_cluster_object(right_object) if right_object is not None else None,
    )


def radar_backed_object(lead: Any) -> ClusterObject | None:
  if not bool(getattr(lead, "status", False)) or not bool(getattr(lead, "radar", False)):
    return None

  track_id = int(getattr(lead, "radarTrackId", -1))
  distance = float(getattr(lead, "dRel", 0.0))
  lateral = float(getattr(lead, "yRel", 0.0))
  relative_speed = float(getattr(lead, "vRel", 0.0))
  if track_id < 0 or distance <= MIN_OBJECT_DISTANCE or \
     not all(math.isfinite(v) for v in (distance, lateral, relative_speed)):
    return None
  return ClusterObject(track_id, distance, lateral, relative_speed)


def validate_slots_for_output(slots: ClusterObjectSlots, lead_one: Any, v_ego: float,
                              standstill: bool) -> ClusterObjectSlots:
  """Fail closed when held display state outlives current fusion or motion."""
  fused_primary = radar_backed_object(lead_one)
  primary = slots.primary
  if fused_primary is None or primary is None or primary.track_id != fused_primary.track_id:
    primary = None

  def valid_side(obj: ClusterObject | None) -> ClusterObject | None:
    if obj is None:
      return None
    if standstill or abs(float(v_ego)) <= 0.1:
      return obj if obj.motion_confirmed else None
    relative_speed = obj.raw_relative_speed if obj.raw_relative_speed is not None else obj.relative_speed
    return obj if abs(float(v_ego) + relative_speed) >= SIDE_MOVING_OBJECT_MIN_SPEED else None

  return ClusterObjectSlots(
    primary,
    valid_side(slots.left),
    valid_side(slots.right),
    valid_side(slots.left_rear),
    valid_side(slots.right_rear),
  )
