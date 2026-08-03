from dataclasses import dataclass


CANFD_NATIVE_BLINDSPOT_STALE_NS = 100_000_000
EV9_RAW_BLINDSPOT_STALE_NS = 150_000_000
BLINDSPOT_WARNING_FLASH_SAMPLES = 20
BLINDSPOT_WARNING_FLASH_ON_SAMPLES = 16
BLINDSPOT_WARNING_SOUND_SAMPLES = 36


@dataclass(frozen=True)
class BlindspotWarningOutput:
  mirror_lamp_active: bool = False
  sound_active: bool = False


@dataclass(frozen=True)
class EV9BlindspotWarningInputs:
  source_fresh: bool = False
  left_detected: bool = False
  right_detected: bool = False
  left_stalk_active: bool = False
  right_stalk_active: bool = False


@dataclass
class BlindspotWarningState:
  flash_phase: int = 0
  mirror_warning_active: bool = False
  escalated_prev: bool = False
  sound_remaining: int = 0
  sound_armed: bool = True


def initialize_ev9_blindspot_state(car_state) -> None:
  car_state.native_left_blindspot_state = 0
  car_state.native_right_blindspot_state = 0
  car_state.native_blindspot_ts = 0
  car_state.native_blindspot_fresh = False
  car_state.ev9_raw_blindspot_state = 0
  car_state.ev9_raw_blindspot_ts = 0
  car_state.ev9_raw_blindspot_fresh = False
  car_state.ev9_reconstructed_left_blindspot = False
  car_state.ev9_reconstructed_right_blindspot = False
  car_state.ev9_reconstructed_blindspot_ts = 0


def update_ev9_canfd_blindspot_state(car_state, cp, ret, enable_bsm: bool) -> None:
  car_state.ev9_raw_blindspot_state = int(cp.vl["BLINDSPOTS_FRONT_CORNER_2"]["SIDE_DETECT_STATE"])
  car_state.ev9_raw_blindspot_ts = cp.ts_nanos["BLINDSPOTS_FRONT_CORNER_2"]["CHECKSUM"]
  wheel_timestamp = cp.ts_nanos["WHEEL_SPEEDS"]["CHECKSUM"]
  raw_age_nanos = max(wheel_timestamp, car_state.ev9_raw_blindspot_ts) - car_state.ev9_raw_blindspot_ts
  car_state.ev9_raw_blindspot_fresh = car_state.ev9_raw_blindspot_ts > 0 and \
    0 <= raw_age_nanos <= EV9_RAW_BLINDSPOT_STALE_NS

  if not enable_bsm:
    return
  car_state.native_left_blindspot_state = int(cp.vl["BLINDSPOTS_REAR_CORNERS"]["BCW_LtIndSta"])
  car_state.native_right_blindspot_state = int(cp.vl["BLINDSPOTS_REAR_CORNERS"]["BCW_RtIndSta"])
  car_state.native_blindspot_ts = cp.ts_nanos["BLINDSPOTS_REAR_CORNERS"]["CHECKSUM"]
  ret.leftBlindspot, ret.rightBlindspot, car_state.native_blindspot_fresh = resolve_canfd_native_blindspot_state(
    car_state.native_left_blindspot_state, car_state.native_right_blindspot_state,
    car_state.native_blindspot_ts, max(wheel_timestamp, car_state.native_blindspot_ts),
  )


def decode_canfd_blinker_stalks(left_stalk: int, right_stalk: int) -> tuple[bool, bool]:
  return int(left_stalk) == 1, int(right_stalk) == 1


def resolve_canfd_native_blindspot_state(left_state: int, right_state: int, timestamp_nanos: int,
                                          now_nanos: int) -> tuple[bool, bool, bool]:
  age_nanos = int(now_nanos) - int(timestamp_nanos)
  fresh = int(timestamp_nanos) > 0 and 0 <= age_nanos <= CANFD_NATIVE_BLINDSPOT_STALE_NS
  if not fresh:
    return False, False, False

  # Native 0x1BA state 1 is the steady lamp and state 2 is the escalated warning.
  # State 0 is neutral and state 3 is not a validated object indication.
  return int(left_state) in (1, 2), int(right_state) in (1, 2), True


def update_blindspot_warning(state: BlindspotWarningState, escalated: bool,
                             blinker: bool) -> BlindspotWarningOutput:
  if not blinker:
    state.flash_phase = 0
    state.mirror_warning_active = False
    state.escalated_prev = False
    state.sound_remaining = 0
    state.sound_armed = True
    return BlindspotWarningOutput()

  rising = escalated and not state.escalated_prev
  if rising:
    state.flash_phase = 0
    state.mirror_warning_active = True
    if state.sound_armed:
      state.sound_remaining = BLINDSPOT_WARNING_SOUND_SAMPLES
      state.sound_armed = False
  elif escalated:
    state.flash_phase = (state.flash_phase + 1) % BLINDSPOT_WARNING_FLASH_SAMPLES
    state.mirror_warning_active = True
  elif state.mirror_warning_active and state.flash_phase < BLINDSPOT_WARNING_FLASH_ON_SAMPLES - 1:
    state.flash_phase += 1
  else:
    state.flash_phase = 0
    state.mirror_warning_active = False

  state.escalated_prev = escalated
  sound_active = state.sound_remaining > 0
  if sound_active:
    state.sound_remaining -= 1
  return BlindspotWarningOutput(
    mirror_lamp_active=state.mirror_warning_active and state.flash_phase < BLINDSPOT_WARNING_FLASH_ON_SAMPLES,
    sound_active=sound_active,
  )


def get_ev9_blindspot_warning_inputs(CS, now_nanos: int) -> EV9BlindspotWarningInputs:
  native_timestamp = int(getattr(CS, "native_blindspot_ts", 0))
  native_age = int(now_nanos) - native_timestamp
  source_fresh = native_timestamp > 0 and 0 <= native_age <= CANFD_NATIVE_BLINDSPOT_STALE_NS
  if source_fresh:
    left_detected = int(getattr(CS, "native_left_blindspot_state", 0)) in (1, 2)
    right_detected = int(getattr(CS, "native_right_blindspot_state", 0)) in (1, 2)
  else:
    fallback_timestamp = int(getattr(CS, "ev9_reconstructed_blindspot_ts", 0))
    fallback_age = int(now_nanos) - fallback_timestamp
    source_fresh = fallback_timestamp > 0 and 0 <= fallback_age <= EV9_RAW_BLINDSPOT_STALE_NS
    if not source_fresh:
      return EV9BlindspotWarningInputs()
    left_detected = bool(getattr(CS, "ev9_reconstructed_left_blindspot", False))
    right_detected = bool(getattr(CS, "ev9_reconstructed_right_blindspot", False))

  left_stalk_active = bool(getattr(CS, "left_blinker_stalk", False))
  right_stalk_active = bool(getattr(CS, "right_blinker_stalk", False))
  if left_stalk_active and right_stalk_active:
    left_stalk_active = False
    right_stalk_active = False

  return EV9BlindspotWarningInputs(
    source_fresh=True,
    left_detected=left_detected,
    right_detected=right_detected,
    left_stalk_active=left_stalk_active,
    right_stalk_active=right_stalk_active,
  )


# Transitional name used by the first extraction; retain it for local callers.
get_blindspot_warning_inputs = get_ev9_blindspot_warning_inputs
