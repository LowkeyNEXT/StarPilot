#!/usr/bin/env python3
from dataclasses import dataclass
import os
import time

from cereal import car
from openpilot.common.swaglog import cloudlog
from opendbc.car.can_definitions import CanData
from opendbc.car.hyundai import ev9_preinit_canfd
from opendbc.car.hyundai.ev9_preinit import EV9PandaPreinitHandoff, EV9PandaPreinitOwner, \
                                               attempt_ev9_pre_fingerprint_suppression, ev9_panda_preinit_armed, \
                                               update_ev9_panda_preinit_handoff
from opendbc.car.hyundai.values import CAR
from openpilot.selfdrive.pandad import can_list_to_can_capnp
from openpilot.selfdrive.car.ev9_preinit import (
  EV9PreinitFaultHistory,
  EV9PreinitOffSample,
  EV9PreinitTakeoverState,
  collect_ev9_preinit_baselines,
  collect_ev9_preinit_claim_receipts,
  complete_ev9_preinit_baselines,
  complete_ev9_preinit_claim_receipts,
  ensure_ev9_preinit_claim_retries,
  ev9_preinit_classify_off_sample,
  ev9_preinit_expected_off_transition,
  ev9_preinit_expected_safety_rejection,
  ev9_preinit_health_snapshot,
  ev9_preinit_health_unchanged,
  ev9_preinit_off_reclaim_failed,
  ev9_preinit_off_reclaim_ready,
  ev9_preinit_allows_fw_query,
  ev9_preinit_parser_packets,
  ev9_preinit_refreshed_takeover_allowed,
  ev9_preinit_recovered_fault_dwell_complete,
  ev9_preinit_safety_ready,
  ev9_preinit_resident_ignition_on,
  ev9_preinit_terminal_ignition_on,
  ev9_preinit_warm_start_pending,
  ev9_panda_faulted_for_actuation,
  load_cached_car_params,
  load_cached_starpilot_car_params,
  normalize_ev9_cached_starpilot_safety,
  revalidate_ev9_panda_preinit_handoff,
)


REPLAY = "REPLAY" in os.environ
EV9_PANDA_PREINIT_SAFETY_TIMEOUT_S = 5.0
EV9_PANDA_PREINIT_CLAIM_TIMEOUT_S = 3.0
EV9_PANDA_PREINIT_CLAIM_RETRY_S = 0.003
EV9_PANDA_PREINIT_CLAIM_POLL_MS = 1
EV9_PANDA_PREINIT_RECLAIM_TIMEOUT_S = 4.0
EV9_PANDA_PREINIT_STATUS_TIMEOUT_S = 0.5
EV9_PANDA_PREINIT_HANDOFF_TIMEOUT_S = 0.5


@dataclass(frozen=True)
class EV9FingerprintStartup:
  handoff: EV9PandaPreinitHandoff
  allow_fw_query: bool
  cached_fpcp: object | None
  pre_fingerprint_suppressed: bool


class EV9PreinitCoordinator:
  """Host-side owner transition kept separate from the generic card loop."""

  @staticmethod
  def initialize(card) -> None:
    card.interface_initialized = False
    card.ev9_preinit_enabled = False
    card.ev9_early_control_active = False
    card.ev9_early_car_control = car.CarControl.new_message().as_reader()
    card.ev9_preinit_takeover_state = EV9PreinitTakeoverState.INACTIVE
    card.ev9_preinit_last_status_time = 0.0
    card.ev9_preinit_cycle_started_us = 0
    card.ev9_preinit_claim_last_host_tx_us = 0
    card.ev9_preinit_health_baseline = None
    card.ev9_preinit_health_pending = None
    card.ev9_preinit_safety_quarantine = False
    card.ev9_preinit_rejected_outputs: list[tuple[float, CanData]] = []
    card.ev9_preinit_claim_receipts = set()
    card.ev9_preinit_claim_templates: dict[tuple[int, int], CanData] = {}
    card.ev9_preinit_claim_started = 0.0
    card.ev9_preinit_off_high_pending = False
    card.ev9_preinit_off_high_pending_started = 0.0
    card.ev9_preinit_resume_fresh_can = False
    card.ev9_preinit_fault_history = EV9PreinitFaultHistory()

  def preinit_startup_enabled(self, cached_params) -> bool:
    """Gate the alternate startup path before generic fingerprinting is changed."""
    return bool(
      cached_params is not None and
      cached_params.carFingerprint == CAR.KIA_EV9 and
      cached_params.brand == "hyundai" and
      cached_params.openpilotLongitudinalControl and
      not cached_params.pcmCruise and
      ev9_panda_preinit_armed(self.params)
    )

  def resolve_preinit_startup_cache(self, cached_params):
    """Recover the persistent EV9 identity after manager clears its ephemeral cache."""
    if self.preinit_startup_enabled(cached_params) or not ev9_panda_preinit_armed(self.params):
      return cached_params

    persistent = load_cached_car_params(self.params, keys=("CarParamsPersistent", "CarParamsPrevRoute"))
    if self.preinit_startup_enabled(persistent):
      cloudlog.warning("EV9 preinit recovered the persistent startup cache")
      return persistent
    return cached_params

  def prepare_fingerprint_startup(self, initial_can_messages: list[CanData], panda_states_event,
                                  cached_params) -> EV9FingerprintStartup:
    self.ev9_preinit_enabled = self.preinit_startup_enabled(cached_params)
    if not self.ev9_preinit_enabled:
      raise RuntimeError("preinit startup preparation called while the feature is disabled")
    panda_states = panda_states_event.pandaStates
    handoff = update_ev9_panda_preinit_handoff(panda_states)
    handoff_deadline = time.monotonic() + EV9_PANDA_PREINIT_HANDOFF_TIMEOUT_S
    while (handoff.owner in (EV9PandaPreinitOwner.NONE, EV9PandaPreinitOwner.PANDA_PENDING) or
           ev9_preinit_warm_start_pending(True, handoff, panda_states)) and time.monotonic() < handoff_deadline:
      timeout_ms = max(1, min(100, int((handoff_deadline - time.monotonic()) * 1000)))
      self.sm.update(timeout_ms)
      if self.sm.updated['pandaStates']:
        panda_states = self.sm['pandaStates']
        handoff = update_ev9_panda_preinit_handoff(panda_states)
    cloudlog.warning(f"EV9 Panda preinit startup decision: {handoff.reason}")

    cached_fpcp = normalize_ev9_cached_starpilot_safety(
      cached_params, load_cached_starpilot_car_params(self.params),
    )
    suppressed = bool(cached_fpcp is not None and attempt_ev9_pre_fingerprint_suppression(
      cached_params, self.params, initial_can_messages,
    ))
    return EV9FingerprintStartup(
      handoff=handoff,
      allow_fw_query=ev9_preinit_allows_fw_query(self.params, handoff),
      cached_fpcp=cached_fpcp,
      pre_fingerprint_suppressed=suppressed,
    )

  def start_early_control(self, handoff: EV9PandaPreinitHandoff | None) -> None:
    requested = bool(ev9_panda_preinit_armed(self.params) and not self.CP.passive and
                     self.CP.carFingerprint == CAR.KIA_EV9 and
                     self.CP.openpilotLongitudinalControl)
    self.ev9_preinit_enabled = requested
    if not requested:
      return

    cloudlog.warning("EV9 production early interface initialization requested")
    refreshed_handoff = handoff or EV9PandaPreinitHandoff()
    if self.params.get_bool("EV9LongPreinitPanda"):
      refreshed_handoff = revalidate_ev9_panda_preinit_handoff(self.sm)
      if not refreshed_handoff.adoptable:
        cloudlog.error(f"EV9 Panda preinit changed before host takeover: {refreshed_handoff.reason}")
    self._initialize_ev9_interface_early()
    self.ev9_early_control_active = self.CP.openpilotLongitudinalControl and not self.params.get_bool("EcuDisableFailed")
    self.CI.CS.ev9_preinit_active = self.ev9_early_control_active
    takeover = ev9_preinit_refreshed_takeover_allowed(
      ev9_panda_preinit_armed(self.params), refreshed_handoff, self.ev9_early_control_active,
    )
    self.params.put_bool_nonblocking("ControlsReady", True)
    if self.ev9_early_control_active and takeover:
      self._prepare_ev9_panda_takeover()
    elif self.ev9_early_control_active:
      self._send_ev9_early_inactive_reconstruction(valid=False)
      cloudlog.warning("EV9 legacy early inactive reconstruction primed")
    cloudlog.warning(f"EV9 early inactive reconstruction active={self.ev9_early_control_active}")

  def _initialize_ev9_interface_early(self) -> None:
    """Initialize only the EV9 preinit path before the normal controls-ready edge."""
    if self.interface_initialized:
      return

    was_openpilot_long = self.CP.openpilotLongitudinalControl
    self.CI.init(self.CP, *self.can_callbacks)
    if was_openpilot_long and self.params.get_bool("EcuDisableFailed"):
      long_flag = 4  # HyundaiSafetyFlags.LONG
      for cfg in self.CP.safetyConfigs:
        cfg.safetyParam &= ~long_flag
      for cfg in self.FPCP.safetyConfigs:
        cfg.safetyParam &= ~long_flag
      self.CP.pcmCruise = True
      self.CP.openpilotLongitudinalControl = False
      self.params.put("CarParams", self.CP.to_bytes())
      self.params.put("StarPilotCarParams", self.FPCP.to_bytes())
    self.interface_initialized = True

  def collect_claim_receipts_from_can_list(self, can_list) -> None:
    if not self.ev9_preinit_enabled:
      return
    if self.ev9_preinit_takeover_state != EV9PreinitTakeoverState.CLAIMING:
      return
    for _, frames in can_list:
      collect_ev9_preinit_claim_receipts(
        self.ev9_preinit_claim_receipts,
        [CanData(address, dat, src) for address, dat, src in frames],
      )

  def update_runtime_state(self, CS, _RD) -> None:
    if not self.ev9_preinit_enabled:
      return
    self._update_ev9_panda_takeover()
    CS.adasUnavailable = self.ev9_preinit_takeover_state == EV9PreinitTakeoverState.FAULTED
    self.CI.CS.openpilot_radar_valid = self.sm.seen['radarState'] and self.sm.alive['radarState'] and self.sm.valid['radarState']

    if self.CP.carFingerprint != CAR.KIA_EV9:
      return
    self.ev9_preinit_fault_history.update(self.sm['pandaStates'])
    panda_faulted = ev9_panda_faulted_for_actuation(
      self.sm['pandaStates'], self.sm.seen['pandaStates'],
      self.ev9_preinit_fault_history.recovered_fault_authorized,
    )
    self.CI.CS.panda_faulted = panda_faulted or self.ev9_preinit_safety_quarantine
    CS.adasUnavailable = bool(self.CP.openpilotLongitudinalControl and
                              (self.ev9_preinit_takeover_state == EV9PreinitTakeoverState.FAULTED or panda_faulted))

  def update_aol_state(self, FPCS) -> None:
    if self.ev9_preinit_enabled:
      self.CI.CS.ev9_always_on_lateral_enabled = bool(FPCS.alwaysOnLateralEnabled)

  def handle_control_step(self, CS, initialized: bool) -> bool:
    if not self.ev9_preinit_enabled:
      return False
    resume_fresh_can = self.ev9_preinit_resume_fresh_can
    self.ev9_preinit_resume_fresh_can = False
    if resume_fresh_can or self.ev9_preinit_takeover_state in (
      EV9PreinitTakeoverState.FAULTED, EV9PreinitTakeoverState.OFF, EV9PreinitTakeoverState.WAIT_SAFETY,
    ):
      return True
    if self.ev9_preinit_takeover_state == EV9PreinitTakeoverState.CLAIMING:
      self._send_ev9_early_inactive_reconstruction(CS.canValid)
      return True
    if self.CP.passive or initialized:
      return False
    if self.ev9_early_control_active:
      self._send_ev9_early_inactive_reconstruction(CS.canValid)
      return True
    return False

  def _send_ev9_early_inactive_reconstruction(self, valid: bool) -> bool:
    """Maintain the complete non-actuating EV9 replacement set during startup."""
    now_nanos = getattr(self, "can_log_mono_time", 0) if REPLAY else int(time.monotonic() * 1e9)
    self.last_actuators_output, can_sends = self.CI.apply(self.ev9_early_car_control, now_nanos, self.starpilot_toggles)
    if self.ev9_preinit_takeover_state == EV9PreinitTakeoverState.CLAIMING:
      # Native controller scheduling is slower than wall time during this
      # blocking startup loop. Retry the complete frozen neutral set so every
      # stream crosses Panda's per-address 90%-period admission window. Panda
      # still canonicalizes counter/CRC, runs safety, and queues only due TX.
      if not ensure_ev9_preinit_claim_retries(can_sends, self.ev9_preinit_claim_templates):
        self._fault_ev9_panda_takeover("complete neutral claim template unavailable")
        return False
    self.pm.send('sendcan', can_list_to_can_capnp(can_sends, msgtype='sendcan', valid=valid))
    return True

  def _send_ev9_panda_claim_retries(self) -> bool:
    """Send only the frozen neutral ownership set during the 333 Hz claim."""
    can_sends = []
    if not ensure_ev9_preinit_claim_retries(can_sends, self.ev9_preinit_claim_templates):
      self._fault_ev9_panda_takeover("complete neutral claim template unavailable")
      return False
    # Do not advance the full CarController at 333 Hz. Its unmanaged keepalive
    # schedules are defined for card's normal 100 Hz loop; Panda needs only
    # these 15 managed tuples plus Tester Present to establish ownership.
    self.pm.send('sendcan', can_list_to_can_capnp(can_sends, msgtype='sendcan', valid=True))
    return True

  @staticmethod
  def _resident_ev9_status(panda_states):
    resident = []
    for panda_state in panda_states or ():
      status = getattr(panda_state, "ev9LongPreinitStatus", None)
      if status is not None and bool(getattr(status, "resident", False)):
        resident.append(status)
    return resident[0] if len(resident) == 1 else None

  def _expected_ev9_panda_safety(self):
    safety_config = self.CP.safetyConfigs[0]
    safety_param = int(safety_config.safetyParam)
    if len(self.FPCP.safetyConfigs):
      safety_param |= int(self.FPCP.safetyConfigs[0].safetyParam)
    alternative_experience = int(self.CP.alternativeExperience) | int(self.FPCP.alternativeExperience)
    return safety_config.safetyModel, safety_param, alternative_experience

  def _drain_ev9_preinit_can(self, baselines: dict | None = None, claim_receipts: set | None = None) -> None:
    packets = self.can_callbacks[0](wait_for_one=False)
    if not packets:
      return
    now = time.monotonic()
    if baselines is not None:
      for packet in packets:
        collect_ev9_preinit_baselines(baselines, packet, now)
    if claim_receipts is not None:
      for packet in packets:
        collect_ev9_preinit_claim_receipts(claim_receipts, packet)
    # Keep CarState's parsers current while the resident bridge owns output so
    # the first neutral host body uses current brake/angle/gear observations.
    self.CI.update(ev9_preinit_parser_packets(packets, now), self.starpilot_toggles)

  def _fault_ev9_panda_takeover(self, reason: str) -> None:
    if self.ev9_preinit_takeover_state == EV9PreinitTakeoverState.FAULTED:
      return
    self.ev9_preinit_takeover_state = EV9PreinitTakeoverState.FAULTED
    self.ev9_preinit_claim_templates = {}
    self.ev9_preinit_health_pending = None
    self.ev9_preinit_safety_quarantine = False
    getattr(self, "ev9_preinit_rejected_outputs", []).clear()
    self.ev9_early_control_active = False
    self.params.put_bool("EcuDisableFailed", True)
    controller = getattr(self.CI, "CC", None)
    if controller is not None:
      controller.ecu_disable_failed = True
      controller.long_active_ecu = False
    cloudlog.error(f"EV9 Panda takeover faulted: {reason}")

  def _enter_ev9_panda_off(self, handoff, terminal_ignition_on: bool | None, now: float) -> None:
    self.ev9_preinit_takeover_state = EV9PreinitTakeoverState.OFF
    self.ev9_preinit_claim_receipts.clear()
    self.ev9_preinit_claim_templates = {}
    self.ev9_preinit_health_pending = None
    self.ev9_preinit_safety_quarantine = False
    getattr(self, "ev9_preinit_rejected_outputs", []).clear()
    self.ev9_preinit_claim_started = 0.0
    self.ev9_preinit_off_high_pending = terminal_ignition_on is True
    self.ev9_preinit_off_high_pending_started = now if self.ev9_preinit_off_high_pending else 0.0
    self.ev9_preinit_last_status_time = now
    cloudlog.warning(f"EV9 Panda takeover entered expected OFF state: {handoff.reason}")

  def _run_ev9_panda_claim(self, timeout_s: float, resume_fresh_can: bool = False) -> bool:
    """Claim every managed stream at a bounded phase-walking cadence."""
    expected_model, expected_param, expected_alternative_experience = self._expected_ev9_panda_safety()
    self.ev9_preinit_takeover_state = EV9PreinitTakeoverState.CLAIMING
    self.ev9_preinit_claim_receipts = set()
    self.ev9_preinit_claim_started = time.monotonic()
    claim_deadline = self.ev9_preinit_claim_started + timeout_s
    # Prime the frozen cache exactly once from the normal 100 Hz controller.
    # This aligns inactive angle and pedal fields with the parsers refreshed
    # during resident-baseline collection. Subsequent 333 Hz batches contain
    # only the frozen managed ownership set and cannot accelerate unrelated
    # controller keepalives.
    if not self._send_ev9_early_inactive_reconstruction(valid=True):
      return False
    next_send = time.monotonic() + EV9_PANDA_PREINIT_CLAIM_RETRY_S
    while time.monotonic() < claim_deadline:
      now = time.monotonic()
      if now >= next_send:
        if not self._send_ev9_panda_claim_retries():
          return False
        # Three milliseconds is deliberately incommensurate with every
        # managed 10/20/50/200 ms cadence. It walks even a hostile fixed phase
        # through Panda's final 10% admission window without relaxing the
        # resident on-wire deadline or relying on scheduler jitter.
        next_send = now + EV9_PANDA_PREINIT_CLAIM_RETRY_S
      self._drain_ev9_preinit_can(claim_receipts=self.ev9_preinit_claim_receipts)
      self.sm.update(EV9_PANDA_PREINIT_CLAIM_POLL_MS)
      if self.sm.updated['pandaStates']:
        panda_states = self.sm['pandaStates']
        latest_handoff = update_ev9_panda_preinit_handoff(panda_states)
        latest_status = self._resident_ev9_status(panda_states)
        off_sample = ev9_preinit_classify_off_sample(
          latest_handoff, panda_states, self.ev9_preinit_cycle_started_us,
        )
        terminal_ignition_on = ev9_preinit_terminal_ignition_on(latest_handoff, panda_states)
        resident_ignition_on = ev9_preinit_resident_ignition_on(latest_handoff, panda_states)
        if ev9_preinit_expected_off_transition(
          self.ev9_preinit_takeover_state, off_sample, terminal_ignition_on, resident_ignition_on,
        ):
          # The expected terminal edge must precede purge-induced safety,
          # fault, and immutable-health deltas, just as it does at runtime.
          self._enter_ev9_panda_off(latest_handoff, resident_ignition_on, time.monotonic())
          return False
        if not self.ev9_preinit_fault_history.update(panda_states):
          self._fault_ev9_panda_takeover("Panda acquired a new or unsupported fault during host claim")
          return False
        timing_valid = latest_status is not None and bool(getattr(latest_status, "timingValid", False))
        if timing_valid:
          self.ev9_preinit_last_status_time = time.monotonic()
        if not ev9_preinit_safety_ready(
          panda_states, expected_model, expected_param, expected_alternative_experience,
        ):
          self._fault_ev9_panda_takeover("Panda safety regressed during host claim")
          return False
        if not ev9_preinit_health_unchanged(panda_states, self.ev9_preinit_health_baseline):
          self._fault_ev9_panda_takeover("Panda CAN error/overflow counters changed during host claim")
          return False
        if not timing_valid:
          continue
        if latest_handoff.owner == EV9PandaPreinitOwner.HOST and latest_status is not None and \
            int(getattr(latest_status, "lastHostTxUs", 0)) != self.ev9_preinit_claim_last_host_tx_us and \
            complete_ev9_preinit_claim_receipts(self.ev9_preinit_claim_receipts):
          self.ev9_preinit_takeover_state = EV9PreinitTakeoverState.CONFIRMED
          if resume_fresh_can:
            self.ev9_preinit_resume_fresh_can = True
          cloudlog.warning("EV9 Panda takeover confirmed by fresh HANDOFF")
          return True
        if latest_handoff.owner not in (EV9PandaPreinitOwner.PANDA, EV9PandaPreinitOwner.HOST):
          self._fault_ev9_panda_takeover(latest_handoff.reason)
          return False
      sleep_for = next_send - time.monotonic()
      if sleep_for > 0.0:
        time.sleep(min(sleep_for, EV9_PANDA_PREINIT_CLAIM_POLL_MS / 1000.0))

    self._fault_ev9_panda_takeover("fresh HANDOFF timed out")
    return False

  def _prepare_ev9_panda_takeover(self) -> None:
    self.ev9_preinit_takeover_state = EV9PreinitTakeoverState.WAIT_SAFETY
    self.ev9_preinit_claim_templates = {}
    self.ev9_preinit_health_pending = None
    self.ev9_preinit_safety_quarantine = False
    getattr(self, "ev9_preinit_rejected_outputs", []).clear()
    expected_model, expected_param, expected_alternative_experience = self._expected_ev9_panda_safety()
    baselines = {}
    deadline = time.monotonic() + EV9_PANDA_PREINIT_SAFETY_TIMEOUT_S
    latest_handoff = None
    latest_status = None
    previous_owner = EV9PandaPreinitOwner.NONE
    safety_ready_since = None

    while time.monotonic() < deadline:
      self._drain_ev9_preinit_can(baselines)
      self.sm.update(10)
      if self.sm.updated['pandaStates']:
        now = time.monotonic()
        panda_states = self.sm['pandaStates']
        latest_handoff = update_ev9_panda_preinit_handoff(panda_states)
        latest_status = self._resident_ev9_status(panda_states)
        if not self.ev9_preinit_fault_history.update(panda_states, initialize=True):
          self._fault_ev9_panda_takeover("Panda acquired a new or unsupported fault before host claim")
          return
        timing_valid = latest_status is not None and bool(getattr(latest_status, "timingValid", False))
        health_snapshot = ev9_preinit_health_snapshot(panda_states)
        if health_snapshot is None:
          self.ev9_preinit_health_baseline = None
          baselines.clear()
          safety_ready_since = None
          continue
        if self.ev9_preinit_health_baseline is None:
          self.ev9_preinit_health_baseline = health_snapshot
        elif health_snapshot != self.ev9_preinit_health_baseline:
          # Host-off RX overflow and a settling CAN core are allowed only while
          # waiting. Seal a new candidate, discard its bodies, and restart the
          # recovered-fault dwell; claim/runtime changes remain hard faults.
          self.ev9_preinit_health_baseline = health_snapshot
          baselines.clear()
          safety_ready_since = None
          continue
        if not timing_valid:
          time.sleep(0.002)
          continue
        self.ev9_preinit_last_status_time = now
        cycle_started_us = int(getattr(latest_status, "cycleStartedUs", 0)) if latest_status is not None else 0
        if self.ev9_preinit_cycle_started_us and cycle_started_us != self.ev9_preinit_cycle_started_us:
          baselines.clear()
        self.ev9_preinit_cycle_started_us = cycle_started_us
        if previous_owner == EV9PandaPreinitOwner.HOST and latest_handoff.owner == EV9PandaPreinitOwner.PANDA:
          # Returned receipts cannot identify their producer. Drop every stale
          # old-host body when its lease expires and collect one complete set
          # emitted by the resident bridge before seeding the new process.
          baselines.clear()
        previous_owner = latest_handoff.owner

        if latest_handoff.owner not in (EV9PandaPreinitOwner.PANDA, EV9PandaPreinitOwner.HOST):
          self._fault_ev9_panda_takeover(latest_handoff.reason)
          return

        baseline_messages = complete_ev9_preinit_baselines(baselines, now)
        safety_ready = ev9_preinit_safety_ready(
          panda_states, expected_model, expected_param, expected_alternative_experience,
        )
        if not safety_ready:
          safety_ready_since = None
        elif self.ev9_preinit_fault_history.recovered_fault_allowed and safety_ready_since is None:
          safety_ready_since = now
        recovered_fault_stable = ev9_preinit_recovered_fault_dwell_complete(
          self.ev9_preinit_fault_history.recovered_fault_allowed, safety_ready_since, now,
        )
        # A stale HANDOFF can belong to a prior card process. With host output
        # held at the source, wait for its lease to expire back to ACTIVE.
        if latest_handoff.owner == EV9PandaPreinitOwner.PANDA and safety_ready and recovered_fault_stable and \
            baseline_messages is not None:
          # Pull every returned receipt already queued before freezing the
          # host template. Panda independently normalizes the final counter in
          # its TX critical section if a resident deadline lands after this.
          self._drain_ev9_preinit_can(baselines)
          baseline_messages = complete_ev9_preinit_baselines(baselines, time.monotonic())
          if baseline_messages is None:
            continue
          ev9_preinit_canfd.set_adrv_baselines(baseline_messages)
          self.ev9_preinit_claim_templates = {
            (msg.src, msg.address): CanData(msg.address, bytes(msg.dat), msg.src)
            for msg in baseline_messages
          }
          self.ev9_preinit_claim_last_host_tx_us = int(getattr(latest_status, "lastHostTxUs", 0))
          break
      time.sleep(0.002)
    else:
      self._fault_ev9_panda_takeover("final safety or complete resident baseline timed out")
      return

    self._run_ev9_panda_claim(EV9_PANDA_PREINIT_CLAIM_TIMEOUT_S)

  def _update_ev9_panda_takeover(self) -> None:
    if self.ev9_preinit_takeover_state not in (EV9PreinitTakeoverState.CLAIMING,
                                                EV9PreinitTakeoverState.CONFIRMED,
                                                EV9PreinitTakeoverState.OFF):
      return
    now = time.monotonic()
    if self.sm.updated['pandaStates']:
      panda_states = self.sm['pandaStates']
      handoff = update_ev9_panda_preinit_handoff(panda_states)
      latest_status = self._resident_ev9_status(panda_states)
      off_sample = ev9_preinit_classify_off_sample(handoff, panda_states, self.ev9_preinit_cycle_started_us)
      terminal_ignition_on = ev9_preinit_terminal_ignition_on(handoff, panda_states)
      resident_ignition_on = ev9_preinit_resident_ignition_on(handoff, panda_states)
      if self.ev9_preinit_takeover_state == EV9PreinitTakeoverState.OFF:
        if off_sample == EV9PreinitOffSample.SAME_TERMINAL_LOW:
          self.ev9_preinit_off_high_pending = False
          self.ev9_preinit_off_high_pending_started = 0.0
          self.ev9_preinit_last_status_time = now
        elif off_sample == EV9PreinitOffSample.SAME_TERMINAL_HIGH:
          if self.ev9_preinit_off_high_pending:
            self._fault_ev9_panda_takeover("repeated ignition-high terminal status after OFF edge")
          else:
            self.ev9_preinit_off_high_pending = True
            self.ev9_preinit_off_high_pending_started = now
          self.ev9_preinit_last_status_time = now
        elif off_sample == EV9PreinitOffSample.FRESH_PENDING:
          # A new firmware generation supersedes a torn high publication from
          # the completed cycle, but remains output-quiescent until ACTIVE.
          self.ev9_preinit_off_high_pending = False
          self.ev9_preinit_off_high_pending_started = 0.0
          self.ev9_preinit_last_status_time = now
        elif off_sample == EV9PreinitOffSample.NONE and terminal_ignition_on is True and \
            not self.ev9_preinit_off_high_pending:
          # The health and status pages can straddle the raw OFF edge. Quarantine
          # on the valid terminal page immediately, but require the next coherent
          # generation publication before confirming, faulting, or reclaiming.
          self.ev9_preinit_off_high_pending = True
          self.ev9_preinit_off_high_pending_started = now
          self.ev9_preinit_last_status_time = now
        elif ev9_preinit_off_reclaim_ready(
          self.ev9_preinit_takeover_state, handoff, panda_states, self.ev9_preinit_cycle_started_us,
        ):
          # A different resident cycle invalidates every health snapshot, host
          # receipt, and recovered-fault allowance from the completed epoch.
          # Re-enter the original blocking takeover path; it emits nothing until
          # exact safety and a complete new resident baseline are established.
          self.ev9_preinit_fault_history = EV9PreinitFaultHistory()
          self.ev9_preinit_health_baseline = None
          self.ev9_preinit_health_pending = None
          self.ev9_preinit_safety_quarantine = False
          getattr(self, "ev9_preinit_rejected_outputs", []).clear()
          self.ev9_preinit_claim_receipts.clear()
          self.ev9_preinit_claim_last_host_tx_us = 0
          self.ev9_preinit_claim_started = 0.0
          self.ev9_preinit_last_status_time = 0.0
          self.ev9_preinit_off_high_pending = False
          self.ev9_preinit_off_high_pending_started = 0.0
          cloudlog.warning("EV9 Panda takeover observed a fresh warm-start epoch; preparing a new host claim")
          self._prepare_ev9_panda_takeover()
          if self.ev9_preinit_takeover_state == EV9PreinitTakeoverState.CONFIRMED:
            self.ev9_preinit_resume_fresh_can = True
        elif ev9_preinit_off_reclaim_failed(
          self.ev9_preinit_takeover_state, handoff, panda_states, self.ev9_preinit_cycle_started_us,
        ):
          self._fault_ev9_panda_takeover(f"fresh warm-start epoch failed: {handoff.reason}")
        if self.ev9_preinit_takeover_state == EV9PreinitTakeoverState.OFF and \
            self.ev9_preinit_off_high_pending and \
            now - self.ev9_preinit_off_high_pending_started > EV9_PANDA_PREINIT_STATUS_TIMEOUT_S:
          self._fault_ev9_panda_takeover("ignition-high OFF edge did not receive a coherent confirming Panda status")
        # Pending, incoherent, and same-cycle samples remain output-quiescent. Only a
        # coherent ACTIVE Panda-owned generation may re-enter takeover; a
        # coherent new FAILED generation becomes a permanent fail-closed fault.
        return
      if ev9_preinit_expected_off_transition(
        self.ev9_preinit_takeover_state, off_sample, terminal_ignition_on, resident_ignition_on,
      ):
        # A terminal page-0 state ends the current ownership epoch even when its
        # paired timing page or ignition health came from the adjacent publish.
        # Quarantine before evaluating the purge-induced immutable health delta.
        self._enter_ev9_panda_off(handoff, resident_ignition_on, now)
        return
      if self.ev9_preinit_takeover_state == EV9PreinitTakeoverState.CONFIRMED:
        # Handoff is one-way for this ignition epoch. From here, use the normal
        # host output and Panda safety behavior; resident ownership is rearmed
        # only by the firmware's ignition-OFF transition.
        self.ev9_preinit_last_status_time = now
        return
      if not self.ev9_preinit_fault_history.update(panda_states):
        self._fault_ev9_panda_takeover("Panda acquired a new or unsupported fault")
        return
      timing_valid = latest_status is not None and bool(getattr(latest_status, "timingValid", False))
      expected_model, expected_param, expected_alternative_experience = self._expected_ev9_panda_safety()
      if not ev9_preinit_safety_ready(
        panda_states, expected_model, expected_param, expected_alternative_experience,
      ):
        self._fault_ev9_panda_takeover("Panda safety/status health regressed")
      elif not ev9_preinit_health_unchanged(panda_states, self.ev9_preinit_health_baseline):
        health_snapshot = ev9_preinit_health_snapshot(panda_states)
        health_pending = getattr(self, "ev9_preinit_health_pending", None)
        if health_pending is not None:
          if health_snapshot == health_pending:
            self.ev9_preinit_health_baseline = health_snapshot
            self.ev9_preinit_health_pending = None
            self.ev9_preinit_safety_quarantine = False
            getattr(self, "ev9_preinit_rejected_outputs", []).clear()
            cloudlog.warning("EV9 Panda safety-rejection counter stabilized; host continuation retained")
          else:
            advanced = ev9_preinit_expected_safety_rejection(
              panda_states, self.ev9_preinit_health_baseline,
              getattr(self, "ev9_preinit_rejected_outputs", []),
            )
            if advanced is not None and advanced != health_pending:
              # Route 1a4 published +1 while two receipts were already queued;
              # route 1a6 similarly published +4 before the fifth receipt.
              # Keep actuation quarantined and follow the monotonic counter
              # until one complete 10 Hz status publication is stable.
              self.ev9_preinit_health_pending = advanced
              self.ev9_preinit_last_status_time = now
              cloudlog.warning("EV9 Panda safety-rejection counter advanced within bounded receipt burst")
            else:
              self._fault_ev9_panda_takeover("Panda safety-rejection counter did not stabilize")
        else:
          pending = ev9_preinit_expected_safety_rejection(
            panda_states, self.ev9_preinit_health_baseline,
            getattr(self, "ev9_preinit_rejected_outputs", []),
          )
          if pending is not None:
            self.ev9_preinit_health_pending = pending
            self.ev9_preinit_safety_quarantine = True
            self.ev9_preinit_last_status_time = now
            cloudlog.warning("EV9 Panda rejected bounded angle output; quarantining actuation until stable")
          else:
            self._fault_ev9_panda_takeover("Panda CAN error/overflow counters changed")
      elif not timing_valid:
        if now - self.ev9_preinit_last_status_time > EV9_PANDA_PREINIT_STATUS_TIMEOUT_S:
          self._fault_ev9_panda_takeover("fresh Panda timing proof timed out")
      elif handoff.owner == EV9PandaPreinitOwner.HOST:
        self.ev9_preinit_last_status_time = now
        if self.ev9_preinit_takeover_state == EV9PreinitTakeoverState.CLAIMING and latest_status is not None and \
            int(getattr(latest_status, "lastHostTxUs", 0)) != self.ev9_preinit_claim_last_host_tx_us and \
            complete_ev9_preinit_claim_receipts(self.ev9_preinit_claim_receipts):
          self.ev9_preinit_takeover_state = EV9PreinitTakeoverState.CONFIRMED
      elif handoff.owner == EV9PandaPreinitOwner.PANDA:
        self.ev9_preinit_last_status_time = now
        if self.ev9_preinit_takeover_state == EV9PreinitTakeoverState.CONFIRMED:
          self.ev9_preinit_claim_last_host_tx_us = int(getattr(latest_status, "lastHostTxUs", 0))
          self._run_ev9_panda_claim(EV9_PANDA_PREINIT_RECLAIM_TIMEOUT_S, resume_fresh_can=True)
          return
      else:
        self._fault_ev9_panda_takeover(handoff.reason)
      if self.ev9_preinit_takeover_state == EV9PreinitTakeoverState.CLAIMING and \
          now - self.ev9_preinit_claim_started > EV9_PANDA_PREINIT_RECLAIM_TIMEOUT_S:
        self._fault_ev9_panda_takeover("fresh runtime HANDOFF timed out")
    elif self.ev9_preinit_takeover_state == EV9PreinitTakeoverState.OFF:
      if self.ev9_preinit_off_high_pending and \
          now - self.ev9_preinit_off_high_pending_started > EV9_PANDA_PREINIT_STATUS_TIMEOUT_S:
        self._fault_ev9_panda_takeover("ignition-high OFF edge did not receive a confirming Panda status")
    elif self.ev9_preinit_takeover_state == EV9PreinitTakeoverState.CLAIMING and \
        now - self.ev9_preinit_last_status_time > EV9_PANDA_PREINIT_STATUS_TIMEOUT_S:
      self._fault_ev9_panda_takeover("fresh Panda status timed out")
