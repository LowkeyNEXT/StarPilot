from types import SimpleNamespace

from openpilot.selfdrive.controls.lib.lane_visibility import model_lane_visibility


def test_model_lane_visibility_uses_model_lane_line_probabilities():
  assert model_lane_visibility(SimpleNamespace(laneLineProbs=[0.1, 0.7, 0.2, 0.1])) == (True, False)
  assert model_lane_visibility(SimpleNamespace(laneLineProbs=[0.1, 0.2, 0.8, 0.1])) == (False, True)
  assert model_lane_visibility(SimpleNamespace(laneLineProbs=[])) == (False, False)
