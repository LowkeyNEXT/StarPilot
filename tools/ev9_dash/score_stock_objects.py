#!/usr/bin/env python3
"""Replay the EV9 display-only object tracker against native stock CCNC output.

The scorer consumes full rlogs, decodes MRR35 and CCNC 0x161/0x162, replays
the production tracker at the radar scan boundary, and compares primary/left/
right slot presence and range. Quote input globs so this process—not the
shell—owns route ordering, for example::

  ./dev python tools/ev9_dash/score_stock_objects.py \
    --stock '/routes/stock-rlogs/000000d[456]--*/rlog.zst' \
    --holdout '/routes/captures/00000128--route--*/rlog.zst'

Ground truth is restricted to the stock TARGET=3 display-active envelope.
This is intentional: 0x162 object fields can remain populated while the stock
UI is inactive, and older captures do not all expose current CarState Main
decoding. Spatial association is approximate (normalized 1.5 m range / 0.8 m
lateral / 2 m/s relative-speed cost), so report both presence and <=5 m
identity metrics. The score is display-only evidence, never BSM or planning
truth. Model-probability confidence can fall in darkness or bad weather; the
daytime corpus cannot establish night robustness.

Production side output is fail-closed by default because route 128 contains
strict-qualified left tracks that native CCNC does not render. Left/right
metrics below are therefore experimental shadow/A-B metrics unless
``--experimental-side-output`` is explicitly supplied.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass, field
import glob
import json
import math
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np

from opendbc.can.parser import CANParser
from opendbc.car.hyundai.ev9_dash import ClusterObjectSlots, Ev9DashObjectTracker
from opendbc.car.hyundai.radar_interface import ev9_dash_display_candidate, ev9_dash_side_candidate, \
  ev9_dash_side_retention_candidate
from openpilot.tools.lib.logreader import LogReader


RADAR_ADDRS = tuple(range(0x3A5, 0x3C5))
RADAR_NAMES = [f"RADAR_TRACK_{a:x}" for a in RADAR_ADDRS]
SLOTS = ("primary", "left", "right")
NATIVE = {
  "primary": ("LEAD", 0),
  "left": ("LEAD_LEFT", 1),
  "right": ("LEAD_RIGHT", -1),
}


def fdiv(n: int | float, d: int | float) -> float:
  return float(n) / float(d) if d else float("nan")


def nearest(points, d: float, y: float, vr: float | None = None):
  choices = []
  for point in points:
    cost = ((float(point.dRel) - d) / 1.5) ** 2 + ((float(point.yRel) - y) / .8) ** 2
    if vr is not None and math.isfinite(vr):
      cost += ((float(point.vRel) - vr) / 2.0) ** 2
    choices.append((cost, point))
  return min(choices, default=None, key=lambda item: item[0])


@dataclass
class SlotFrame:
  route: str
  time: float
  slot: str
  truth: bool
  native_distance: float
  predicted: bool
  track_id: int = -1
  raw_distance: float = math.nan
  ema_distance: float = math.nan
  segment: str = ""
  route_time: float = math.nan
  raw_lateral: float = math.nan
  relative_speed: float = math.nan
  world_speed: float = math.nan
  score: float = math.nan
  model_prob: float = math.nan
  track_hits: int = 0
  side_entry_hits: int = 0
  strict_state_3: int = 0
  strict_state_12: int = 0
  strict_state_15: int = 0
  strict_state_17: int = 0
  native_left: bool = False
  native_right: bool = False
  radar_fields: dict[str, float] = field(default_factory=dict)


@dataclass
class RouteResult:
  frames: list[SlotFrame] = field(default_factory=list)
  candidate_counts: Counter = field(default_factory=Counter)
  fused_counts: Counter = field(default_factory=Counter)
  fused_scores: list[float] = field(default_factory=list)
  fused_laterals: list[float] = field(default_factory=list)
  context_counts: Counter = field(default_factory=Counter)


class RouteEvaluator:
  def __init__(self, route: str):
    self.route = route
    self.radar = CANParser("hyundai_mrr35_radar_generated", [(name, 20) for name in RADAR_NAMES], 0)
    self.ccnc = CANParser("hyundai_canfd_generated", [("CCNC_0x161", 20), ("CCNC_0x162", 20)], 1)
    self.tracker = Ev9DashObjectTracker()
    self.points_by_addr = {}
    self.next_track_id = 0
    self.updated_addrs = set()
    self.slots = ClusterObjectSlots()
    self.slot_time = 0
    self.v_ego = 0.0
    self.standstill = True
    self.main_available = False
    self.drive_gear = False
    self.latest_fused = None
    self.latest_fused_time = 0
    self.latest_native = None
    self.latest_native_time = 0
    self.native_target_active = False
    self.result = RouteResult()
    self.current_segment = ""
    self.route_start_nanos = None
    self.slot_meta = {}
    self.left_entry_hits = {}

  def update_car_state(self, event):
    cs = event.carState
    self.v_ego = float(cs.vEgo)
    self.standstill = bool(cs.standstill)
    self.main_available = bool(cs.cruiseState.available)
    self.drive_gear = str(cs.gearShifter) == "drive"

  def update_fused(self, event):
    lead = event.radarState.leadOne
    self.latest_fused_time = int(event.logMonoTime)
    self.latest_fused = SimpleNamespace(
      status=bool(lead.status), radar=bool(lead.radar), dRel=float(lead.dRel),
      yRel=float(lead.yRel), vRel=float(lead.vRel),
      modelProb=float(lead.modelProb),
    )

  def update_can(self, event):
    stamp = int(event.logMonoTime)
    frames = [(int(c.address), bytes(c.dat), int(c.src)) for c in event.can if int(c.src) < 128]
    radar_updated = self.radar.update([stamp, frames])
    self.updated_addrs.update(a for a in radar_updated if a in RADAR_ADDRS)
    ccnc_updated = self.ccnc.update([stamp, frames])
    if 0x161 in ccnc_updated:
      self.native_target_active = int(self.ccnc.vl["CCNC_0x161"]["TARGET"]) == 3

    if 0x3C4 in self.updated_addrs:
      self.run_scan(stamp)
      self.updated_addrs.clear()
    if 0x162 in ccnc_updated:
      self.latest_native = dict(self.ccnc.vl["CCNC_0x162"])
      self.latest_native_time = stamp
      self.score_native(stamp)

  def run_scan(self, stamp: int):
    self.result.context_counts["scans"] += 1
    self.result.context_counts["radar_valid"] += self.radar.can_valid
    self.result.context_counts["main"] += self.main_available
    self.result.context_counts["drive"] += self.drive_gear
    # Mirror RadarInterface's address lifecycle and monotonically assigned IDs.
    for address in RADAR_ADDRS:
      values = self.radar.vl[f"RADAR_TRACK_{address:x}"]
      if int(values["STATE"]) in (3, 4):
        point = self.points_by_addr.get(address)
        if point is None:
          point = SimpleNamespace(trackId=self.next_track_id, measured=True)
          self.next_track_id += 1
          self.points_by_addr[address] = point
        point.dRel = float(values["LONG_DIST"])
        point.yRel = float(values["LAT_DIST"])
        point.vRel = float(values["REL_SPEED"])
      else:
        self.points_by_addr.pop(address, None)

    points = list(self.points_by_addr.values())
    display, side, retention = set(), set(), set()
    for address in self.updated_addrs:
      point = self.points_by_addr.get(address)
      if point is None:
        continue
      values = self.radar.vl[f"RADAR_TRACK_{address:x}"]
      if ev9_dash_display_candidate(values):
        display.add(point.trackId)
      if ev9_dash_side_candidate(values):
        side.add(point.trackId)
      if ev9_dash_side_retention_candidate(values):
        retention.add(point.trackId)

    for point in points:
      moving = abs(self.v_ego + float(point.vRel)) >= 2.78
      left_entry = point.trackId in side and 1.8 < float(point.yRel) < 4.0 and \
        float(point.dRel) < 80.0 and moving
      self.left_entry_hits[point.trackId] = self.left_entry_hits.get(point.trackId, 0) + 1 if left_entry else 0

    preferred = -1
    fused = self.latest_fused
    if fused is not None and fused.status and fused.radar and stamp - self.latest_fused_time <= 150_000_000:
      match = nearest(points, fused.dRel, fused.yRel, fused.vRel)
      if match is not None and match[0] <= 9.0:
        preferred = int(match[1].trackId)
        self.result.fused_counts["matched"] += 1
        # Recover the address to report discriminator distribution of the
        # exact fused physical track.
        address = next(a for a, p in self.points_by_addr.items() if p is match[1])
        values = self.radar.vl[f"RADAR_TRACK_{address:x}"]
        score = float(values["NEW_SIGNAL_7"])
        self.result.fused_scores.append(score)
        self.result.fused_laterals.append(float(match[1].yRel))
        self.result.fused_counts["display200"] += score > 200
      else:
        self.result.fused_counts["unmatched"] += 1

    # TARGET=3 is the route-native display-active envelope. Old captures do
    # not all expose the current CarState Main decoding (route 128 reports
    # cruiseState.available false while its stock CCNC target is active).
    # This oracle gates scoring only; production still uses live Main/Drive.
    context = self.radar.can_valid and self.native_target_active and self.drive_gear
    self.result.context_counts["context"] += context
    if not context:
      self.slots = self.tracker.clear()
    else:
      model_prob = float(self.latest_fused.modelProb) if preferred >= 0 and self.latest_fused is not None else 0.0
      self.slots = self.tracker.update(points, preferred, model_prob, display, side, retention,
                                       self.v_ego, self.standstill)
    self.slot_meta = {}
    for slot in SLOTS:
      obj = getattr(self.slots, slot)
      if obj is None:
        continue
      tracked = self.tracker.tracks.get(obj.track_id)
      address = next((a for a, p in self.points_by_addr.items() if p.trackId == obj.track_id), None)
      values = self.radar.vl[f"RADAR_TRACK_{address:x}"] if address is not None else {}
      self.slot_meta[slot] = {
        "raw_lateral": float(tracked.raw_lateral) if tracked is not None else math.nan,
        "relative_speed": float(tracked.raw_relative_speed) if tracked is not None else math.nan,
        "world_speed": self.v_ego + float(tracked.raw_relative_speed) if tracked is not None else math.nan,
        "score": float(values.get("NEW_SIGNAL_7", math.nan)),
        "model_prob": float(self.latest_fused.modelProb) if self.latest_fused is not None else math.nan,
        "track_hits": int(tracked.hits) if tracked is not None else 0,
        "side_entry_hits": self.left_entry_hits.get(obj.track_id, 0),
        "strict_state_3": int(values.get("NEW_SIGNAL_3", 0)),
        "strict_state_12": int(values.get("NEW_SIGNAL_12", 0)),
        "strict_state_15": int(values.get("NEW_SIGNAL_15", 0)),
        "strict_state_17": int(values.get("NEW_SIGNAL_17", 0)),
        "radar_fields": {name: float(value) for name, value in values.items()},
      }
    self.slot_time = stamp

    # Candidate precision is reported only as an allow-list diagnostic. A raw
    # candidate is not a selector: stock has one slot but radar can have many.
    native = self.latest_native
    native_fresh = native is not None and stamp - self.latest_native_time <= 150_000_000
    for address in self.updated_addrs:
      point = self.points_by_addr.get(address)
      if point is None:
        continue
      values = self.radar.vl[f"RADAR_TRACK_{address:x}"]
      if not ev9_dash_display_candidate(values):
        continue
      self.result.candidate_counts["predicted"] += 1
      matched = False
      if native_fresh:
        for _slot, (signal, sign) in NATIVE.items():
          if not int(native[signal]):
            continue
          d = float(native[f"{signal}_DISTANCE"]) + .2
          y = 0.0 if sign == 0 else sign * float(native[f"{signal}_LATERAL"])
          if ((point.dRel - d) / 1.5) ** 2 + ((point.yRel - y) / .8) ** 2 <= 9.0:
            matched = True
            break
      self.result.candidate_counts["matched_native"] += matched

  def score_native(self, stamp: int):
    native = self.latest_native
    if not self.native_target_active:
      return
    # Require a fresh completed radar scan, as the real card does. Startup and
    # malformed intervals count as fail-closed misses only after 150 ms.
    fresh = stamp - self.slot_time <= 150_000_000
    for slot, (signal, _sign) in NATIVE.items():
      truth = bool(int(native[signal]))
      native_d = float(native[f"{signal}_DISTANCE"]) if truth else math.nan
      obj = getattr(self.slots, slot) if fresh else None
      predicted = obj is not None
      track_id, raw_d, ema_d = -1, math.nan, math.nan
      if obj is not None:
        track_id = int(obj.track_id)
        tracked = self.tracker.tracks.get(track_id)
        raw_d = float(tracked.raw_distance) - .2 if tracked is not None else math.nan
        ema_d = float(obj.distance) - .2
      self.result.frames.append(SlotFrame(self.route, stamp * 1e-9, slot, truth, native_d,
                                          predicted, track_id, raw_d, ema_d,
                                          segment=self.current_segment,
                                          route_time=(stamp - self.route_start_nanos) * 1e-9 if self.route_start_nanos is not None else math.nan,
                                          native_left=bool(int(native["LEAD_LEFT"])),
                                          native_right=bool(int(native["LEAD_RIGHT"])),
                                          **(self.slot_meta.get(slot, {}) if predicted else {})))


def confusion(frames: list[SlotFrame], match_distance: float | None = None):
  c = Counter()
  for f in frames:
    if f.truth and f.predicted:
      matched = match_distance is None or abs(f.native_distance - f.ema_distance) <= match_distance
      if matched:
        c["tp"] += 1
      else:
        c["fp"] += 1
        c["fn"] += 1
    elif f.predicted:
      c["fp"] += 1
    elif f.truth:
      c["fn"] += 1
    else:
      c["tn"] += 1
  return c


def episodes(frames: list[SlotFrame], field_name: str):
  result, current = [], []
  last_t = None
  for f in frames:
    active = bool(getattr(f, field_name))
    if active and (last_t is None or f.time - last_t <= .15):
      current.append(f)
    elif active:
      if current:
        result.append(current)
      current = [f]
    elif current:
      result.append(current)
      current = []
    last_t = f.time
  if current:
    result.append(current)
  return result


def summarize_lifecycle(frames: list[SlotFrame]):
  truth_eps = episodes(frames, "truth")
  pred_eps = episodes(frames, "predicted")
  acquire = []
  drop = []
  missed = 0
  for truth in truth_eps:
    overlapping = [p for p in pred_eps if p[-1].time >= truth[0].time and p[0].time <= truth[-1].time]
    if not overlapping:
      missed += 1
      continue
    pred = overlapping[0]
    acquire.append(max(0.0, pred[0].time - truth[0].time))
    drop.append(max(0.0, pred[-1].time - truth[-1].time))
  ghosts = []
  for pred in pred_eps:
    if not any(t[-1].time >= pred[0].time and t[0].time <= pred[-1].time for t in truth_eps):
      ghosts.append(pred[-1].time - pred[0].time + .05)
  return truth_eps, pred_eps, acquire, drop, missed, ghosts


def distance_metrics(frames: list[SlotFrame]):
  matched = [f for f in frames if f.truth and f.predicted and abs(f.native_distance - f.raw_distance) <= 5.0]
  if not matched:
    return None
  raw_error = np.asarray([f.raw_distance - f.native_distance for f in matched])
  ema_error = np.asarray([f.ema_distance - f.native_distance for f in matched])

  raw_second, ema_second = [], []
  lag = []
  by_track = defaultdict(list)
  for f in matched:
    by_track[(f.route, f.slot, f.track_id)].append(f)
  for seq in by_track.values():
    seq.sort(key=lambda f: f.time)
    run = []
    for f in seq:
      if run and f.time - run[-1].time > .15:
        if len(run) >= 3:
          raw_second.extend(run[i].raw_distance - 2 * run[i - 1].raw_distance + run[i - 2].raw_distance for i in range(2, len(run)))
          ema_second.extend(run[i].ema_distance - 2 * run[i - 1].ema_distance + run[i - 2].ema_distance for i in range(2, len(run)))
        run = []
      run.append(f)
    if len(run) >= 3:
      raw_second.extend(run[i].raw_distance - 2 * run[i - 1].raw_distance + run[i - 2].raw_distance for i in range(2, len(run)))
      ema_second.extend(run[i].ema_distance - 2 * run[i - 1].ema_distance + run[i - 2].ema_distance for i in range(2, len(run)))
      for i in range(1, len(run) - 1):
        dt = run[i + 1].time - run[i - 1].time
        slope = (run[i + 1].raw_distance - run[i - 1].raw_distance) / dt if dt > 0 else 0.0
        if abs(slope) >= 1.0:
          estimate = -(run[i].ema_distance - run[i].raw_distance) / slope
          if -.1 <= estimate <= .4:
            lag.append(estimate)

  def rms(a):
    x = np.asarray(a)
    return float(np.sqrt(np.mean(x * x))) if len(x) else math.nan
  return {
    "n": len(matched),
    "raw_mae": float(np.mean(np.abs(raw_error))),
    "ema_mae": float(np.mean(np.abs(ema_error))),
    "raw_bias": float(np.mean(raw_error)),
    "ema_bias": float(np.mean(ema_error)),
    "raw_rmse": rms(raw_error),
    "ema_rmse": rms(ema_error),
    "raw_second_rms": rms(raw_second),
    "ema_second_rms": rms(ema_second),
    "jitter_reduction": 1.0 - fdiv(rms(ema_second), rms(raw_second)),
    "empirical_lag_ms": float(np.median(lag) * 1000.0) if lag else math.nan,
  }


def describe(values):
  a = np.asarray([v for v in values if math.isfinite(v)])
  if not len(a):
    return "n/a"
  return "/".join(f"{x:.3f}" for x in np.quantile(a, [0, .1, .5, .9, 1]))


def report_false_primary_episodes(frames: list[SlotFrame]):
  primary = [f for f in frames if f.slot == "primary"]
  truth_eps = episodes(primary, "truth")
  pred_eps = episodes(primary, "predicted")
  ghosts = [p for p in pred_eps if not any(t[-1].time >= p[0].time and t[0].time <= p[-1].time for t in truth_eps)]
  print("  false-only primary episodes (q0/q10/q50/q90/q100):")
  for i, ep in enumerate(ghosts, 1):
    duration = ep[-1].time - ep[0].time + .05
    message = f"    {i}: {ep[0].segment} t={ep[0].route_time:.3f}-{ep[-1].route_time:.3f}s "
    message += f"duration={duration:.3f}s frames={len(ep)} side-native={sum(f.native_left or f.native_right for f in ep)}; "
    message += f"d={describe([f.raw_distance for f in ep])}, y={describe([f.raw_lateral for f in ep])}, "
    message += f"vRel={describe([f.relative_speed for f in ep])}, vWorld={describe([f.world_speed for f in ep])}, "
    message += f"score={describe([f.score for f in ep])}, modelProb={describe([f.model_prob for f in ep])}, "
    message += f"hits={describe([float(f.track_hits) for f in ep])}"
    print(message)


def report_false_side_episodes(frames: list[SlotFrame], slot: str):
  side = [f for f in frames if f.slot == slot]
  truth_eps = episodes(side, "truth")
  pred_eps = episodes(side, "predicted")
  ghosts = [p for p in pred_eps if not any(t[-1].time >= p[0].time and t[0].time <= p[-1].time for t in truth_eps)]
  print(f"  false-only {slot} episodes (q0/q10/q50/q90/q100):")
  for i, ep in enumerate(ghosts, 1):
    duration = ep[-1].time - ep[0].time + .05
    strict = sorted({(f.strict_state_3, f.strict_state_12, f.strict_state_15, f.strict_state_17) for f in ep})
    message = f"    {i}: {ep[0].segment} t={ep[0].route_time:.3f}-{ep[-1].route_time:.3f}s "
    message += f"duration={duration:.3f}s frames={len(ep)}; d={describe([f.raw_distance for f in ep])}, "
    message += f"y={describe([f.raw_lateral for f in ep])}, vWorld={describe([f.world_speed for f in ep])}, "
    message += f"score={describe([f.score for f in ep])}, entryHits={describe([float(f.side_entry_hits) for f in ep])}, "
    message += f"strict(3/12/15/17)={strict}"
    print(message)


def report_cross_dataset_left_gates(stock: list[RouteResult], holdout: list[RouteResult]):
  stock_frames = [f for r in stock for f in r.frames if f.slot == "left" and f.predicted]
  holdout_frames = [f for r in holdout for f in r.frames if f.slot == "left" and f.predicted]
  stock_tp = [f for f in stock_frames if f.truth]
  stock_fp = [f for f in stock_frames if not f.truth]
  holdout_fp = [f for f in holdout_frames if not f.truth]
  if not stock_tp or not holdout_fp:
    return

  print("\nCROSS-DATASET LEFT GATES (stock TP loss / stock FP removed / holdout FP removed):")
  candidates = {
    "distance<=": (lambda f, x: f.raw_distance <= x, [30, 40, 50, 60, 70, 75, 80]),
    "lateral>=": (lambda f, x: f.raw_lateral >= x, [1.9, 2.0, 2.2, 2.4, 2.6, 2.8, 3.0]),
    "lateral<=": (lambda f, x: f.raw_lateral <= x, [3.0, 3.2, 3.4, 3.6, 3.8, 4.0]),
    "score>=": (lambda f, x: f.score >= x, [281, 285, 290, 300, 310, 320]),
    "worldSpeed>=": (lambda f, x: abs(f.world_speed) >= x, [3, 5, 8, 10, 15, 20]),
    "entryHits>=": (lambda f, x: f.side_entry_hits >= x, [3, 4, 5, 10, 20, 40]),
  }
  rows = []
  for label, (predicate, thresholds) in candidates.items():
    for threshold in thresholds:
      tp_loss = 1.0 - fdiv(sum(predicate(f, threshold) for f in stock_tp), len(stock_tp))
      stock_removed = 1.0 - fdiv(sum(predicate(f, threshold) for f in stock_fp), len(stock_fp)) if stock_fp else 0.0
      holdout_removed = 1.0 - fdiv(sum(predicate(f, threshold) for f in holdout_fp), len(holdout_fp))
      if tp_loss <= .03 and holdout_removed >= .1:
        rows.append((tp_loss, -holdout_removed, label, threshold, stock_removed))
  for tp_loss, neg_holdout_removed, label, threshold, stock_removed in sorted(rows)[:20]:
    print(f"  {label}{threshold}: {tp_loss:.3%} / {stock_removed:.3%} / {-neg_holdout_removed:.3%}")

  mined = []
  field_names = sorted(set.intersection(*(set(f.radar_fields) for f in stock_tp + holdout_fp)))
  for name in field_names:
    tp_values = np.asarray([f.radar_fields[name] for f in stock_tp])
    stock_fp_values = np.asarray([f.radar_fields[name] for f in stock_fp]) if stock_fp else np.asarray([])
    holdout_values = np.asarray([f.radar_fields[name] for f in holdout_fp])
    combined = np.concatenate((tp_values, holdout_values))
    thresholds = np.unique(np.quantile(combined, np.linspace(0, 1, 41)))
    for operator, compare in (("<=", np.less_equal), (">=", np.greater_equal)):
      for threshold in thresholds:
        tp_loss = 1.0 - float(np.mean(compare(tp_values, threshold)))
        stock_removed = 1.0 - float(np.mean(compare(stock_fp_values, threshold))) if len(stock_fp_values) else 0.0
        holdout_removed = 1.0 - float(np.mean(compare(holdout_values, threshold)))
        if tp_loss <= .05 and holdout_removed >= .1:
          mined.append((tp_loss, -holdout_removed, name, operator, float(threshold), stock_removed))
    for value in np.unique(holdout_values):
      tp_loss = float(np.mean(tp_values == value))
      stock_removed = float(np.mean(stock_fp_values == value)) if len(stock_fp_values) else 0.0
      holdout_removed = float(np.mean(holdout_values == value))
      if tp_loss <= .05 and holdout_removed >= .1:
        mined.append((tp_loss, -holdout_removed, name, "!=", float(value), stock_removed))
  print("  mined MRR35 single-field gates with <=5% stock TP loss / >=10% holdout removal:")
  for tp_loss, neg_holdout_removed, name, operator, threshold, stock_removed in sorted(mined)[:30]:
    print(f"    {name}{operator}{threshold:g}: {tp_loss:.3%} / {stock_removed:.3%} / {-neg_holdout_removed:.3%}")


def report_simple_gates(frames: list[SlotFrame]):
  predicted = [f for f in frames if f.slot == "primary" and f.predicted]
  tp = [f for f in predicted if f.truth]
  fp = [f for f in predicted if not f.truth]
  if not tp or not fp:
    return
  print("  predicted-primary feature q0/q10/q50/q90/q100 (true vs false):")
  for name in ("raw_distance", "raw_lateral", "relative_speed", "world_speed", "score", "model_prob", "track_hits"):
    true_values = [abs(float(getattr(f, name))) if name == "raw_lateral" else float(getattr(f, name)) for f in tp]
    false_values = [abs(float(getattr(f, name))) if name == "raw_lateral" else float(getattr(f, name)) for f in fp]
    print(f"    {name}: true={describe(true_values)} false={describe(false_values)}")

  gates = []
  candidates = {
    "abs(y)<=": (lambda f, x: abs(f.raw_lateral) <= x, [.5, 1.0, 1.5, 2.0, 2.2, 2.5, 3.0]),
    "distance<=": (lambda f, x: f.raw_distance <= x, [40, 60, 80, 100, 120, 150]),
    "score>=": (lambda f, x: f.score >= x, [1, 4, 25, 50, 100, 150, 200]),
    "modelProb>=": (lambda f, x: f.model_prob >= x, [.1, .25, .5, .75, .9]),
    "hits>=": (lambda f, x: f.track_hits >= x, [3, 4, 5, 10, 20, 40]),
  }
  for label, (predicate, thresholds) in candidates.items():
    for threshold in thresholds:
      kept_tp = sum(predicate(f, threshold) for f in tp)
      kept_fp = sum(predicate(f, threshold) for f in fp)
      tp_loss = 1.0 - fdiv(kept_tp, len(tp))
      fp_removed = 1.0 - fdiv(kept_fp, len(fp))
      if fp_removed >= .1 and tp_loss <= .05:
        gates.append((tp_loss, -fp_removed, label, threshold, kept_tp, kept_fp))
  print("  simple gates with >=10% FP removal and <=5% TP loss:")
  for tp_loss, neg_fp_removed, label, threshold, kept_tp, kept_fp in sorted(gates)[:12]:
    print(f"    {label}{threshold}: TP loss={tp_loss:.3%}, FP removed={-neg_fp_removed:.3%}, kept={kept_tp}/{kept_fp}")


def finite_or_none(value):
  return float(value) if isinstance(value, (float, np.floating)) and math.isfinite(value) else \
    (int(value) if isinstance(value, (int, np.integer)) else None)


def confusion_summary(c: Counter):
  return {
    "precision": finite_or_none(fdiv(c["tp"], c["tp"] + c["fp"])),
    "recall": finite_or_none(fdiv(c["tp"], c["tp"] + c["fn"])),
    "tp": c["tp"], "fp": c["fp"], "fn": c["fn"], "tn": c["tn"],
  }


def dataset_summary(name: str, results: list[RouteResult], experimental_side_output: bool):
  frames = [f for result in results for f in result.frames]
  slots = {}
  for slot in SLOTS:
    sf = [f for f in frames if f.slot == slot]
    truth_eps, pred_eps, acquire, drop, missed, ghosts = summarize_lifecycle(sf)
    shadow = {
      "presence": confusion_summary(confusion(sf)),
      "identity_within_5m": confusion_summary(confusion(sf, 5.0)),
      "lifecycle": {
        "truth_episodes": len(truth_eps), "predicted_episodes": len(pred_eps),
        "missed_episodes": missed, "false_only_episodes": len(ghosts),
        "max_false_only_seconds": finite_or_none(max(ghosts, default=0.0)),
        "median_acquire_seconds": finite_or_none(np.median(acquire)) if acquire else None,
        "p95_acquire_seconds": finite_or_none(np.quantile(acquire, .95)) if acquire else None,
        "median_dropout_seconds": finite_or_none(np.median(drop)) if drop else None,
        "p95_dropout_seconds": finite_or_none(np.quantile(drop, .95)) if drop else None,
      },
      "distance": {key: finite_or_none(value) for key, value in (distance_metrics(sf) or {}).items()},
    }
    if slot in ("left", "right") and not experimental_side_output:
      truth_count = sum(f.truth for f in sf)
      slots[slot] = {
        "production_mode": "suppressed",
        "production_presence": {"precision": None, "recall": 0.0 if truth_count else None,
                                "tp": 0, "fp": 0, "fn": truth_count},
        "experimental_shadow": shadow,
      }
    else:
      slots[slot] = shadow

  candidate = sum((r.candidate_counts for r in results), Counter())
  fused = sum((r.fused_counts for r in results), Counter())
  return {
    "name": name,
    "routes": len(results),
    "native_0x162_frames_in_target3": len(frames) // 3,
    "slots": slots,
    "raw_candidate_diagnostic": {
      "matched_native_precision": finite_or_none(fdiv(candidate["matched_native"], candidate["predicted"])),
      "matched_native": candidate["matched_native"], "predicted": candidate["predicted"],
    },
    "fused_match": {
      "matched": fused["matched"], "unmatched": fused["unmatched"],
      "score_over_200_fraction": finite_or_none(fdiv(fused["display200"], fused["matched"])),
    },
  }


def report(name: str, results: list[RouteResult], experimental_side_output: bool, verbose: bool):
  frames = [f for result in results for f in result.frames]
  print(f"\nDATASET {name}: {len(results)} routes, {len(frames) // 3} native 0x162 frames")
  if not experimental_side_output:
    print("  production side output: SUPPRESSED (0 predicted side frames/ghosts); left/right below are shadow metrics")
  for slot in SLOTS:
    sf = [f for f in frames if f.slot == slot]
    c = confusion(sf)
    identity = confusion(sf, 5.0)
    label = f"{slot} (experimental shadow)" if slot in ("left", "right") and not experimental_side_output else slot
    message = f"  {label}: presence P/R={fdiv(c['tp'], c['tp'] + c['fp']):.4f}/{fdiv(c['tp'], c['tp'] + c['fn']):.4f} "
    message += f"(tp/fp/fn={c['tp']}/{c['fp']}/{c['fn']}), identity<=5m P/R="
    message += f"{fdiv(identity['tp'], identity['tp'] + identity['fp']):.4f}/"
    message += f"{fdiv(identity['tp'], identity['tp'] + identity['fn']):.4f}"
    print(message)
    truth_eps, pred_eps, acq, drop, missed, ghosts = summarize_lifecycle(sf)
    message = f"    episodes truth/pred/missed/ghost={len(truth_eps)}/{len(pred_eps)}/{missed}/{len(ghosts)}, "
    message += f"acquire median/p95={np.median(acq) if acq else math.nan:.3f}/"
    message += f"{np.quantile(acq, .95) if acq else math.nan:.3f}s, "
    message += f"drop median/p95={np.median(drop) if drop else math.nan:.3f}/"
    message += f"{np.quantile(drop, .95) if drop else math.nan:.3f}s, ghost max={max(ghosts, default=0):.3f}s"
    print(message)
    dm = distance_metrics(sf)
    if dm:
      print("    distance: " + ", ".join(f"{k}={v:.4f}" if isinstance(v, float) else f"{k}={v}" for k, v in dm.items()))

  candidate = sum((r.candidate_counts for r in results), Counter())
  fused = sum((r.fused_counts for r in results), Counter())
  scores = [x for r in results for x in r.fused_scores]
  laterals = [x for r in results for x in r.fused_laterals]
  contexts = sum((r.context_counts for r in results), Counter())
  message = f"  raw >200 candidate matched-native precision={fdiv(candidate['matched_native'], candidate['predicted']):.4f} "
  message += f"({candidate['matched_native']}/{candidate['predicted']}; diagnostic only, not a selector)"
  print(message)
  message = f"  fused raw-match={fused['matched']}/{fused['matched'] + fused['unmatched']}, >200 fraction="
  message += f"{fdiv(fused['display200'], fused['matched']):.4f}"
  print(message)
  if scores:
    print("  fused NEW_SIGNAL_7 q0/q10/q50/q90/q99/q100=" + "/".join(
      f"{x:.1f}" for x in np.quantile(scores, [0, .1, .5, .9, .99, 1])))
  print("  scan context fractions radar-valid/main/drive/all=" + "/".join(
    f"{fdiv(contexts[key], contexts['scans']):.4f}" for key in ("radar_valid", "main", "drive", "context")))
  if laterals:
    print("  fused |lateral| q50/q90/q95/q99/max=" + "/".join(
      f"{x:.2f}" for x in np.quantile(np.abs(laterals), [.5, .9, .95, .99, 1])))

  for route in sorted({f.route for f in frames}):
    rf = [f for f in frames if f.route == route]
    parts = []
    for slot in SLOTS:
      c = confusion([f for f in rf if f.slot == slot])
      parts.append(f"{slot}={fdiv(c['tp'], c['tp'] + c['fp']):.3f}/{fdiv(c['tp'], c['tp'] + c['fn']):.3f}")
    print(f"  route {route}: " + ", ".join(parts))
  if verbose and "holdout" in name:
    report_false_primary_episodes(frames)
    report_false_side_episodes(frames, "left")
    report_false_side_episodes(frames, "right")
  if verbose:
    report_simple_gates(frames)


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("--stock", help="d4/d5/d6 rlog, segment/root directory, or quoted glob")
  ap.add_argument("--holdout", help="holdout rlog, segment/root directory, or quoted glob")
  ap.add_argument("--primary-entry", type=float, default=Ev9DashObjectTracker.PRIMARY_ENTRY_HALF_WIDTH)
  ap.add_argument("--primary-retention", type=float, default=Ev9DashObjectTracker.PRIMARY_RETENTION_HALF_WIDTH)
  ap.add_argument("--model-prob-threshold", type=float, default=Ev9DashObjectTracker.PRIMARY_MODEL_PROB_NEAR)
  ap.add_argument("--far-model-prob-threshold", type=float, default=Ev9DashObjectTracker.PRIMARY_MODEL_PROB_FAR)
  ap.add_argument("--far-model-prob-distance", type=float, default=Ev9DashObjectTracker.PRIMARY_MODEL_PROB_FAR_DISTANCE)
  ap.add_argument("--primary-max-distance", type=float, default=Ev9DashObjectTracker.MAX_DISTANCE)
  ap.add_argument("--experimental-side-output", action="store_true",
                  help="label side metrics as enabled A/B output instead of production-suppressed shadow state")
  ap.add_argument("--verbose", action="store_true", help="print false episodes and exploratory one-field gate mining")
  ap.add_argument("--json", action="store_true", help="emit a machine-readable summary")
  args = ap.parse_args()
  if not args.stock and not args.holdout:
    ap.error("at least one of --stock or --holdout is required")
  Ev9DashObjectTracker.MAX_DISTANCE = args.primary_max_distance
  Ev9DashObjectTracker.PRIMARY_ENTRY_HALF_WIDTH = args.primary_entry
  Ev9DashObjectTracker.PRIMARY_RETENTION_HALF_WIDTH = args.primary_retention
  Ev9DashObjectTracker.PRIMARY_MODEL_PROB_NEAR = args.model_prob_threshold
  Ev9DashObjectTracker.PRIMARY_MODEL_PROB_FAR = args.far_model_prob_threshold
  Ev9DashObjectTracker.PRIMARY_MODEL_PROB_FAR_DISTANCE = args.far_model_prob_distance

  def resolve_paths(spec: str):
    path = Path(spec)
    if path.is_file():
      return [path]
    if path.is_dir():
      direct = next((path / name for name in ("rlog.zst", "rlog.bz2", "rlog") if (path / name).is_file()), None)
      return [direct] if direct is not None else [Path(p) for p in glob.glob(str(path / "*" / "rlog*"))]
    return [Path(p) for p in glob.glob(spec)]

  datasets = [(name, spec) for name, spec in (("stock-d4-d6", args.stock), ("holdout-128", args.holdout)) if spec]
  all_results = {}
  for name, pattern in datasets:
    paths = resolve_paths(pattern)
    paths.sort(key=lambda p: (p.parent.name.rsplit("--", 1)[0], int(p.parent.name.rsplit("--", 1)[1])))
    grouped = defaultdict(list)
    for path in paths:
      grouped[path.parent.name.rsplit("--", 1)[0]].append(path)
    results = []
    for route, route_paths in grouped.items():
      print(f"reading {route} ({len(route_paths)} segments)", file=sys.stderr if args.json else sys.stdout, flush=True)
      evaluator = RouteEvaluator(route)
      for path in route_paths:
        evaluator.current_segment = path.parent.name
        for event in LogReader(str(path), sort_by_time=False):
          if evaluator.route_start_nanos is None:
            evaluator.route_start_nanos = int(event.logMonoTime)
          which = event.which()
          if which == "carState":
            evaluator.update_car_state(event)
          elif which == "radarState":
            evaluator.update_fused(event)
          elif which == "can":
            evaluator.update_can(event)
      results.append(evaluator.result)
    all_results[name] = results
    if not args.json:
      report(name, results, args.experimental_side_output, args.verbose)
  if args.verbose and not args.json and {"stock-d4-d6", "holdout-128"} <= set(all_results):
    report_cross_dataset_left_gates(all_results["stock-d4-d6"], all_results["holdout-128"])
  if args.json:
    output = {
      "config": {
        "primary_entry_half_width_m": Ev9DashObjectTracker.PRIMARY_ENTRY_HALF_WIDTH,
        "primary_retention_half_width_m": Ev9DashObjectTracker.PRIMARY_RETENTION_HALF_WIDTH,
        "primary_model_prob_near": Ev9DashObjectTracker.PRIMARY_MODEL_PROB_NEAR,
        "primary_model_prob_far": Ev9DashObjectTracker.PRIMARY_MODEL_PROB_FAR,
        "primary_model_prob_far_distance_m": Ev9DashObjectTracker.PRIMARY_MODEL_PROB_FAR_DISTANCE,
        "acquisition_samples": Ev9DashObjectTracker.ACQUISITION_SAMPLES,
        "dropout_hold_samples": Ev9DashObjectTracker.DROPOUT_HOLD_SAMPLES,
        "ema_alpha": Ev9DashObjectTracker.EMA_ALPHA,
        "experimental_side_output": args.experimental_side_output,
      },
      "datasets": {name: dataset_summary(name, results, args.experimental_side_output)
                   for name, results in all_results.items()},
      "limitations": [
        "Ground truth is restricted to native CCNC TARGET=3 and uses approximate spatial association.",
        "The preserved corpus is daytime and cannot establish model-confidence behavior at night or in poor weather.",
        "Side metrics are experimental shadow metrics unless experimental_side_output is true.",
      ],
    }
    print(json.dumps(output, indent=2, allow_nan=False))


if __name__ == "__main__":
  main()
