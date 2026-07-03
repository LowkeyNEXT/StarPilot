LANE_VISIBLE_PROB = 0.5


def model_lane_visibility(model_v2) -> tuple[bool, bool]:
  lane_line_probs = list(getattr(model_v2, "laneLineProbs", []))
  left_lane_visible = len(lane_line_probs) > 1 and lane_line_probs[1] > LANE_VISIBLE_PROB
  right_lane_visible = len(lane_line_probs) > 2 and lane_line_probs[2] > LANE_VISIBLE_PROB
  return left_lane_visible, right_lane_visible
