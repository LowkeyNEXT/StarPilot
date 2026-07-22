#!/usr/bin/env python3
"""Score the production EV9 raw-BSM fallback against native stock lamps.

Example::

  ./dev python tools/ev9_dash/score_stock_bsm.py \
    --stock '/path/to/stock-rlogs/000000d[456]--*/rlog.zst'
"""

import argparse
from collections import Counter
import glob
from pathlib import Path
import re
from types import SimpleNamespace

from opendbc.can import CANParser
from opendbc.car.hyundai.carstate import EV9_RAW_BLINDSPOT_STALE_NS
from opendbc.car.hyundai.ev9_dash import Ev9RawBlindspotGateState, update_ev9_raw_blindspot_gate
from opendbc.car.hyundai.radar_interface import ev9_dash_display_candidate, ev9_dash_side_candidate
from openpilot.tools.lib.logreader import LogReader


RADAR_ADDRS = tuple(range(0x3A5, 0x3C5))
RADAR_NAMES = [f"RADAR_TRACK_{address:x}" for address in RADAR_ADDRS]
ROUTE_RE = re.compile(r"(?P<route>.+)--(?P<segment>\d+)$")


def confusion(truth: bool, predicted: bool) -> str:
  if truth and predicted:
    return "tp"
  if predicted:
    return "fp"
  if truth:
    return "fn"
  return "tn"


def metrics(counts: Counter) -> tuple[float, float]:
  precision_denominator = counts["tp"] + counts["fp"]
  recall_denominator = counts["tp"] + counts["fn"]
  return (
    counts["tp"] / precision_denominator if precision_denominator else float("nan"),
    counts["tp"] / recall_denominator if recall_denominator else float("nan"),
  )


class SegmentEvaluator:
  def __init__(self) -> None:
    self.radar = CANParser("hyundai_mrr35_radar_generated", [(name, 20) for name in RADAR_NAMES], 0)
    self.bsm = CANParser("hyundai_canfd_generated", [
      ("BLINDSPOTS_REAR_CORNERS", 0),
      ("BLINDSPOTS_FRONT_CORNER_2", 0),
    ], 1)
    self.gate = Ev9RawBlindspotGateState()
    self.points = {}
    self.updated_addrs = set()
    self.raw_state = 0
    self.raw_time = 0
    self.prediction = (False, False)
    self.prediction_time = 0
    self.v_ego = 0.0
    self.drive_gear = False
    self.counts = (Counter(), Counter())

  def update(self, event) -> None:
    which = event.which()
    if which == "carState":
      self.v_ego = float(event.carState.vEgo)
      self.drive_gear = str(event.carState.gearShifter) == "drive"
      return
    if which != "can":
      return

    stamp = int(event.logMonoTime)
    frames = [(int(frame.address), bytes(frame.dat), int(frame.src))
              for frame in event.can if int(frame.src) < 128]
    radar_updated = self.radar.update([stamp, frames])
    self.updated_addrs.update(address for address in radar_updated if address in RADAR_ADDRS)
    bsm_updated = self.bsm.update([stamp, frames])
    if 0x36A in bsm_updated:
      self.raw_state = int(self.bsm.vl["BLINDSPOTS_FRONT_CORNER_2"]["SIDE_DETECT_STATE"])
      self.raw_time = stamp

    if 0x3C4 in self.updated_addrs:
      self._update_gate(stamp)
      self.updated_addrs.clear()
    if 0x1BA in bsm_updated:
      self._score_native(stamp)

  def _update_gate(self, stamp: int) -> None:
    display, side = set(), set()
    for address in RADAR_ADDRS:
      values = self.radar.vl[f"RADAR_TRACK_{address:x}"]
      if int(values["STATE"]) not in (3, 4):
        self.points.pop(address, None)
        continue
      point = self.points.setdefault(address, SimpleNamespace(trackId=address, measured=True))
      point.dRel = float(values["LONG_DIST"])
      point.yRel = float(values["LAT_DIST"])
      point.vRel = float(values["REL_SPEED"])
      if address in self.updated_addrs and ev9_dash_display_candidate(values):
        display.add(address)
      if address in self.updated_addrs and ev9_dash_side_candidate(values):
        side.add(address)

    raw_fresh = self.raw_time > 0 and 0 <= stamp - self.raw_time <= EV9_RAW_BLINDSPOT_STALE_NS
    self.prediction = update_ev9_raw_blindspot_gate(
      self.gate, self.raw_state, raw_fresh, self.drive_gear,
      list(self.points.values()), display, side, self.v_ego,
    )
    self.prediction_time = stamp if raw_fresh else 0

  def _score_native(self, stamp: int) -> None:
    native = self.bsm.vl["BLINDSPOTS_REAR_CORNERS"]
    truth = (
      int(native["BCW_LtIndSta"]) in (1, 2),
      int(native["BCW_RtIndSta"]) in (1, 2),
    )
    fresh = self.prediction_time > 0 and \
      0 <= stamp - self.prediction_time <= EV9_RAW_BLINDSPOT_STALE_NS
    for index in (0, 1):
      self.counts[index][confusion(truth[index], self.prediction[index] if fresh else False)] += 1


def route_and_segment(path: Path) -> tuple[str, int]:
  match = ROUTE_RE.fullmatch(path.parent.name)
  if match is None:
    return path.parent.name, 0
  return match.group("route"), int(match.group("segment"))


def print_counts(label: str, counts: tuple[Counter, Counter]) -> None:
  print(label)
  for name, values in zip(("left", "right"), counts, strict=True):
    precision, recall = metrics(values)
    result = f"  {name}: P/R={precision:.4f}/{recall:.4f} "
    result += f"(tp/fp/fn={values['tp']}/{values['fp']}/{values['fn']})"
    print(result)


def main() -> None:
  parser = argparse.ArgumentParser()
  parser.add_argument("--stock", required=True, help="quoted rlog path or glob")
  args = parser.parse_args()
  paths = sorted((Path(path) for path in glob.glob(args.stock)), key=route_and_segment)
  if not paths:
    raise RuntimeError(f"no rlogs matched {args.stock!r}")

  totals = (Counter(), Counter())
  route_totals = {}
  for path in paths:
    route, segment = route_and_segment(path)
    print(f"reading {route} segment {segment:02d}")
    evaluator = SegmentEvaluator()
    for event in LogReader(str(path), sort_by_time=False):
      evaluator.update(event)
    route_counts = route_totals.setdefault(route, (Counter(), Counter()))
    for index in (0, 1):
      totals[index].update(evaluator.counts[index])
      route_counts[index].update(evaluator.counts[index])

  print_counts("all routes", totals)
  for route, counts in route_totals.items():
    print_counts(route, counts)


if __name__ == "__main__":
  main()
