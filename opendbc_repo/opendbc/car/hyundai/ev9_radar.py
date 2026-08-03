from dataclasses import dataclass, field

from opendbc.car.hyundai.ev9_dash import Ev9DashTrackCandidates
from openpilot.common.swaglog import cloudlog


EV9_CLUSTER_DISPLAY_DISCRIMINATOR_SIGNAL = "UNKNOWN_7"
EV9_CLUSTER_DISPLAY_DISCRIMINATOR_THRESHOLD = 200.0
EV9_CLUSTER_STRICT_SIDE_DISCRIMINATOR_THRESHOLD = 280.0
EV9_CLUSTER_DISPLAY_DISCRIMINATOR_REJECT_LOG_LIMIT = 5


def _ev9_dash_display_discriminator(values) -> float:
  # The route-analysis DBC used NEW_SIGNAL_7; production still uses UNKNOWN_7.
  return float(values.get("NEW_SIGNAL_7", values.get(EV9_CLUSTER_DISPLAY_DISCRIMINATOR_SIGNAL, 0.0)))


def ev9_dash_display_candidate(values) -> bool:
  return _ev9_dash_display_discriminator(values) > EV9_CLUSTER_DISPLAY_DISCRIMINATOR_THRESHOLD


def ev9_dash_side_candidate(values) -> bool:
  return int(values.get("NEW_SIGNAL_3", 0)) == 2 and \
         int(values.get("NEW_SIGNAL_12", 0)) == 10 and \
         int(values.get("NEW_SIGNAL_15", 0)) == 2 and \
         int(values.get("NEW_SIGNAL_17", 0)) == 1


def ev9_dash_side_retention_candidate(values) -> bool:
  return _ev9_dash_display_discriminator(values) > EV9_CLUSTER_STRICT_SIDE_DISCRIMINATOR_THRESHOLD or \
         (int(values.get("NEW_SIGNAL_3", 0)) == 2 and int(values.get("NEW_SIGNAL_17", 0)) == 1)


def mrr35_cluster_display_candidate(values) -> bool:
  return ev9_dash_display_candidate(values)


def mrr35_strict_side_display_candidate(values) -> bool:
  return ev9_dash_side_candidate(values)


def mrr35_side_display_retention_candidate(values) -> bool:
  return ev9_dash_side_retention_candidate(values)


@dataclass
class EV9RadarDisplayTracker:
  quality_track_ids: set[int] = field(default_factory=set)
  strict_side_track_ids: set[int] = field(default_factory=set)
  side_retention_track_ids: set[int] = field(default_factory=set)
  discriminator_reject_count: int = 0
  discriminator_reject_logs: int = 0

  def reset(self) -> Ev9DashTrackCandidates:
    self.begin_update()
    return Ev9DashTrackCandidates()

  def begin_update(self) -> None:
    self.quality_track_ids.clear()
    self.strict_side_track_ids.clear()
    self.side_retention_track_ids.clear()

  def update_track(self, values, track_id: int, address: int, updated: bool) -> None:
    if not updated:
      return
    if ev9_dash_display_candidate(values):
      self.quality_track_ids.add(track_id)
    else:
      self.discriminator_reject_count += 1
      if self.discriminator_reject_logs < EV9_CLUSTER_DISPLAY_DISCRIMINATOR_REJECT_LOG_LIMIT:
        self.discriminator_reject_logs += 1
        cloudlog.warning(
          "EV9 cluster display discriminator rejected MRR35 track: addr=0x%X, discriminator=%.0f, rejected=%d",
          address, _ev9_dash_display_discriminator(values), self.discriminator_reject_count,
        )
    if ev9_dash_side_candidate(values):
      self.strict_side_track_ids.add(track_id)
    if ev9_dash_side_retention_candidate(values):
      self.side_retention_track_ids.add(track_id)

  def finish_update(self, can_valid: bool) -> Ev9DashTrackCandidates:
    if not can_valid:
      self.begin_update()
    return Ev9DashTrackCandidates(
      frozenset(self.quality_track_ids),
      frozenset(self.strict_side_track_ids),
      frozenset(self.side_retention_track_ids),
    )


# Transitional unprefixed names used by the first extraction.
display_discriminator = _ev9_dash_display_discriminator
dash_display_candidate = ev9_dash_display_candidate
dash_side_candidate = ev9_dash_side_candidate
dash_side_retention_candidate = ev9_dash_side_retention_candidate

# Original public names retained from radar_interface.py.
ev9_mrr35_cluster_display_candidate = mrr35_cluster_display_candidate
ev9_mrr35_strict_side_display_candidate = mrr35_strict_side_display_candidate
ev9_mrr35_side_display_retention_candidate = mrr35_side_display_retention_candidate
