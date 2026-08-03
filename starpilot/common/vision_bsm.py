from __future__ import annotations

import time

from cereal import car


VASM_STATE_TIMEOUT_SECONDS = 3.0
EV9_FINGERPRINT = "KIA_EV9"
EV9_BSM_MASTER_PARAM = "KiaEv9ClusterSideObjectsEnabled"
EV9_ENHANCED_BSM_PARAM = "KiaEv9ClusterEnhancedBsmEnabled"


def _persistent_car_fingerprint(params) -> str:
  try:
    raw = params.get("CarParamsPersistent")
    if raw:
      with car.CarParams.from_bytes(raw) as CP:
        return str(CP.carFingerprint or "")
  except Exception:
    pass
  return ""


def ev9_enhanced_bsm_requested(params, car_fingerprint: str | None = None) -> bool:
  """Enable the EV9-only V-ASM consumer only for its selected Enhanced BSM mode."""
  fingerprint = _persistent_car_fingerprint(params) if car_fingerprint is None else str(car_fingerprint)
  return fingerprint == EV9_FINGERPRINT and params.get_bool(EV9_BSM_MASTER_PARAM) and \
    params.get_bool(EV9_ENHANCED_BSM_PARAM)


def vasm_requested(params, car_fingerprint: str | None = None) -> bool:
  return params.get_bool("VASMEnabled") or ev9_enhanced_bsm_requested(params, car_fingerprint)


def get_fresh_vasm_state(params_memory, now: float | None = None) -> tuple[bool, bool]:
  """Return V-ASM state only while the vision daemon is updating it."""
  try:
    updated_at = float(params_memory.get("VASMLastUpdateMonoTime") or 0)
  except (TypeError, ValueError):
    return False, False

  current_time = time.monotonic() if now is None else now
  age = current_time - updated_at
  if updated_at <= 0 or age < 0 or age > VASM_STATE_TIMEOUT_SECONDS:
    return False, False

  active_values = ("1", b"1", True)
  return params_memory.get("VASMLeftActive") in active_values, params_memory.get("VASMRightActive") in active_values
