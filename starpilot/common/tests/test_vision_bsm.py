from openpilot.starpilot.common.vision_bsm import VASM_STATE_TIMEOUT_SECONDS, get_fresh_vasm_state, vasm_requested


class FakeParams:
  def __init__(self, values):
    self.values = values

  def get(self, key):
    return self.values.get(key)

  def get_bool(self, key):
    return bool(self.values.get(key, False))


def test_fresh_vasm_state_is_returned():
  params = FakeParams({
    "VASMLastUpdateMonoTime": "100.0",
    "VASMLeftActive": "1",
    "VASMRightActive": "0",
  })

  assert get_fresh_vasm_state(params, now=101.0) == (True, False)


def test_stale_or_invalid_vasm_state_fails_closed():
  stale = FakeParams({"VASMLastUpdateMonoTime": "100.0", "VASMLeftActive": "1"})
  invalid = FakeParams({"VASMLastUpdateMonoTime": "invalid", "VASMLeftActive": "1"})

  assert get_fresh_vasm_state(stale, now=100.0 + VASM_STATE_TIMEOUT_SECONDS + 0.01) == (False, False)
  assert get_fresh_vasm_state(invalid, now=100.0) == (False, False)


def test_ev9_enhanced_bsm_requests_vasm_without_global_toggle():
  params = FakeParams({
    "VASMEnabled": False,
    "KiaEv9ClusterSideObjectsEnabled": True,
    "KiaEv9ClusterEnhancedBsmEnabled": True,
  })

  assert vasm_requested(params, "KIA_EV9")
  assert not vasm_requested(params, "KIA_EV6")

  params.values["KiaEv9ClusterEnhancedBsmEnabled"] = False
  assert not vasm_requested(params, "KIA_EV9")


def test_global_vasm_remains_platform_independent():
  assert vasm_requested(FakeParams({"VASMEnabled": True}), "KIA_EV6")
