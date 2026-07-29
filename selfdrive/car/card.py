#!/usr/bin/env python3
import math
import os
import time
import threading

import cereal.messaging as messaging

from cereal import car, custom, log

from openpilot.common.params import Params
from openpilot.common.realtime import config_realtime_process, Priority, Ratekeeper
from openpilot.common.swaglog import cloudlog, ForwardingHandler
from openpilot.system.hardware.hw import Paths

from opendbc.car import DT_CTRL, ButtonType, structs
from opendbc.car.can_definitions import CanData, CanRecvCallable, CanSendCallable
from opendbc.car.carlog import carlog
from opendbc.car.fw_versions import ObdCallback
from opendbc.car.car_helpers import get_car, interfaces
from opendbc.car.hyundai.interface import EV9PandaPreinitHandoff, EV9PandaPreinitOwner, attempt_ev9_pre_fingerprint_suppression, \
                                             ev9_panda_preinit_armed, update_ev9_panda_preinit_handoff
from opendbc.car.interfaces import CarInterfaceBase, RadarInterfaceBase
from opendbc.safety import ALTERNATIVE_EXPERIENCE
from openpilot.selfdrive.pandad import can_capnp_to_list, can_list_to_can_capnp
from openpilot.common.constants import CV
from openpilot.selfdrive.car.cruise import (
  VCruiseHelper, IMPERIAL_INCREMENT, V_CRUISE_MAX, V_CRUISE_MIN,
  is_speed_limit_confirmation_pending,
)
from openpilot.selfdrive.car.redneck_cruise import RedneckCruise, select_redneck_target_speed
from openpilot.selfdrive.car.car_specific import MockCarState
from openpilot.selfdrive.car.ev9_preinit import EV9PreinitTakeoverState, collect_ev9_preinit_claim_receipts, \
                                                ev9_preinit_allows_fw_query, ev9_preinit_refreshed_takeover_allowed, \
                                                load_cached_car_params, load_cached_starpilot_car_params, \
                                                normalize_ev9_cached_starpilot_safety, revalidate_ev9_panda_preinit_handoff, \
                                                ev9_preinit_warm_start_pending
from openpilot.selfdrive.car.ev9_preinit_coordinator import EV9PreinitCoordinator
from openpilot.selfdrive.car.ev9_dash_coordinator import EV9DashCoordinator

from openpilot.starpilot.common.favorite_slots import (
  FAVORITE_ACTION_ACCEL_COUNTER,
  FAVORITE_ACTION_DECEL_COUNTER,
)
from openpilot.starpilot.common.starpilot_variables import get_starpilot_toggles, update_starpilot_toggles
from openpilot.starpilot.controls.starpilot_card import StarPilotCard

REPLAY = "REPLAY" in os.environ
OPENPILOT_LEAD_MIN_DISTANCE = 0.1
REDNECK_DECREASE_LOOKAHEAD_POINTS = 10
EV9_PANDA_PREINIT_HANDOFF_TIMEOUT_S = 0.5

EventName = log.OnroadEvent.EventName

# forward
carlog.addHandler(ForwardingHandler(cloudlog))


def obd_callback(params: Params) -> ObdCallback:
  def set_obd_multiplexing(obd_multiplexing: bool):
    if params.get_bool("ObdMultiplexingEnabled") != obd_multiplexing:
      cloudlog.warning(f"Setting OBD multiplexing to {obd_multiplexing}")
      params.remove("ObdMultiplexingChanged")
      params.put_bool("ObdMultiplexingEnabled", obd_multiplexing)
      params.get_bool("ObdMultiplexingChanged", block=True)
      cloudlog.warning("OBD multiplexing set successfully")
  return set_obd_multiplexing


def can_comm_callbacks(logcan: messaging.SubSocket, sendcan: messaging.PubSocket) -> tuple[CanRecvCallable, CanSendCallable]:
  def can_recv(wait_for_one: bool = False) -> list[list[CanData]]:
    """
    wait_for_one: wait the normal logcan socket timeout for a CAN packet, may return empty list if nothing comes

    Returns: CAN packets comprised of CanData objects for easy access
    """
    ret = []
    for can in messaging.drain_sock(logcan, wait_for_one=wait_for_one):
      ret.append([CanData(msg.address, msg.dat, msg.src) for msg in can.can])
    return ret

  def can_send(msgs: list[CanData]) -> None:
    sendcan.send(can_list_to_can_capnp(msgs, msgtype='sendcan'))

  return can_recv, can_send


def ev9_panda_faulted_for_actuation(panda_states, seen: bool) -> bool:
  """Return the strict Panda-fault interlock used by EV9 actuation.

  Neutral resident/host reconstruction may continue under the separately
  bounded recovered-CAN3 policy so the cluster truthfully reports ADAS
  unavailability. Vehicle actuation is stricter for every current fault, but
  Panda's historical faultTemp status cannot be treated as current after its
  faults bitmap has been cleared by fault_recovered().
  """
  def fault_status_active(panda_state) -> bool:
    # pycapnp dynamic enums intentionally do not implement int(); their stable
    # string representation matches the schema enumerant. Keep integer zero
    # compatibility for lightweight unit-test/fake objects.
    return str(getattr(panda_state, "faultStatus", "none")) in ("faultPerm", "2")

  if not seen or len(panda_states) == 0:
    return True
  for panda_state in panda_states:
    faulted = len(getattr(panda_state, "faults", ())) > 0 or fault_status_active(panda_state)
    if faulted:
      return True
  return False


class Car:
  CI: CarInterfaceBase
  RI: RadarInterfaceBase
  CP: car.CarParams

  FPCP: custom.StarPilotCarParams

  def __init__(self, CI=None, RI=None) -> None:
    self.can_sock = messaging.sub_sock('can', timeout=20)
    self.sm = messaging.SubMaster(['pandaStates', 'carControl', 'onroadEvents', 'radarState', 'longitudinalPlan'])
    self.pm = messaging.PubMaster(['sendcan', 'carState', 'carParams', 'carOutput', 'liveTracks'])

    self.can_rcv_cum_timeout_counter = 0

    self.CC_prev = car.CarControl.new_message()
    self.CS_prev = car.CarState.new_message()
    self.initialized_prev = False
    self.interface_initialized = False
    self.ev9_early_control_active = False
    EV9PreinitCoordinator.initialize(self)
    # CarController expects a reader (normal carControl messages come from a
    # SubMaster). Passing a builder makes its nested actuators lack as_builder.
    self.ev9_early_car_control = car.CarControl.new_message().as_reader()

    self.last_actuators_output = structs.CarControl.Actuators()

    self.params = Params()
    self.params_memory = Params(memory=True)
    self._favorite_virtual_accel_counter = self.params_memory.get_int(FAVORITE_ACTION_ACCEL_COUNTER)
    self._favorite_virtual_decel_counter = self.params_memory.get_int(FAVORITE_ACTION_DECEL_COUNTER)
    self._favorite_virtual_releases = []

    self.can_callbacks = can_comm_callbacks(self.can_sock, self.pm.sock['sendcan'])

    is_release = False
    pre_fingerprint_suppressed = False
    ev9_panda_handoff = EV9PandaPreinitHandoff()

    if CI is None:
      # wait for one pandaState and one CAN packet
      print("Waiting for CAN messages...")
      while True:
        can = messaging.recv_one_retry(self.can_sock)
        if len(can.can) > 0:
          break

      initial_can_messages = [CanData(msg.address, msg.dat, msg.src) for msg in can.can]
      # Preserve any additional frames already queued in the same wake-up
      # burst. This does not wait or widen the UDS timing window.
      for initial_event in messaging.drain_sock(self.can_sock, wait_for_one=False):
        initial_can_messages.extend(CanData(msg.address, msg.dat, msg.src) for msg in initial_event.can)

      alpha_long_allowed = self.params.get_bool("AlphaLongitudinalEnabled")
      panda_states_event = messaging.recv_one_retry(self.sm.sock['pandaStates'])
      num_pandas = len(panda_states_event.pandaStates)
      # Ownership proof is live and boot-scoped. The persistent arm Param only
      # selects firmware and must never be used as a successful-handoff signal.
      ev9_panda_states = panda_states_event.pandaStates
      ev9_panda_handoff = update_ev9_panda_preinit_handoff(ev9_panda_states)
      if self.params.get_bool("EV9LongPreinitPanda"):
        # pandad publishes at 10 Hz, so the first sample can legitimately be a
        # WAIT state while Panda is already bridging tentative deadlines. Wait
        # only on live PandaState—not CAN—and make a terminal fail-closed
        # decision before fingerprinting or any host diagnostic request.
        handoff_deadline = time.monotonic() + EV9_PANDA_PREINIT_HANDOFF_TIMEOUT_S
        while (ev9_panda_handoff.owner in (EV9PandaPreinitOwner.NONE, EV9PandaPreinitOwner.PANDA_PENDING) or
               ev9_preinit_warm_start_pending(True, ev9_panda_handoff, ev9_panda_states)) and \
            time.monotonic() < handoff_deadline:
          timeout_ms = max(1, min(100, int((handoff_deadline - time.monotonic()) * 1000)))
          self.sm.update(timeout_ms)
          if self.sm.updated['pandaStates']:
            ev9_panda_states = self.sm['pandaStates']
            ev9_panda_handoff = update_ev9_panda_preinit_handoff(ev9_panda_states)
        cloudlog.warning(f"EV9 Panda preinit startup decision: {ev9_panda_handoff.reason}")
      allow_fw_query = ev9_preinit_allows_fw_query(self.params, ev9_panda_handoff)

      cached_params = load_cached_car_params(self.params)
      cached_fpcp = load_cached_starpilot_car_params(self.params)
      cached_fpcp = normalize_ev9_cached_starpilot_safety(cached_params, cached_fpcp)

      # If the strictly gated pre-fingerprint request succeeds, use the exact
      # persisted interface configuration instead of spending another second
      # collecting a live fingerprint while the ADAS output is already muted.
      # Both parameter blobs were produced by a verified EV9 route and the UDS
      # helper independently checks identity, firmware, and developer gates.
      if cached_fpcp is not None:
        pre_fingerprint_suppressed = attempt_ev9_pre_fingerprint_suppression(cached_params, self.params, *self.can_callbacks,
                                                                              initial_can_messages)

      if pre_fingerprint_suppressed:
        cloudlog.warning("EV9 using verified persistent interface after pre-fingerprint suppression")
        self.CI = interfaces[cached_params.carFingerprint](cached_params, cached_fpcp)
      else:
        self.CI = get_car(*self.can_callbacks, obd_callback(self.params), alpha_long_allowed, is_release, self.params, num_pandas, cached_params,
                          get_starpilot_toggles(), allow_fw_query=allow_fw_query)
      self.RI = interfaces[self.CI.CP.carFingerprint].RadarInterface(self.CI.CP)
      self.CP = self.CI.CP

      # continue onto next fingerprinting step in pandad
      self.params.put_bool("FirmwareQueryDone", True)

      self.FPCP = self.CI.FPCP
    else:
      self.CI, self.CP, self.FPCP = CI, CI.CP, CI.FPCP
      self.RI = RI

    interface_alternative_experience = self.CP.alternativeExperience
    self.CP.alternativeExperience = interface_alternative_experience
    openpilot_enabled_toggle = self.params.get_bool("OpenpilotEnabledToggle")
    controller_available = self.CI.CC is not None and openpilot_enabled_toggle
    self.CP.passive = not controller_available
    if self.CP.passive:
      safety_config = structs.CarParams.SafetyConfig()
      safety_config.safetyModel = structs.CarParams.SafetyModel.noOutput
      self.CP.safetyConfigs = [safety_config]

    if self.CP.secOcRequired and not is_release:
      # Copy user key if available
      try:
        user_key = Params(Paths.params_cache_root()).get("SecOCKey")
        if user_key is not None:
          user_key = user_key.strip()
          if len(user_key) == 32:
            self.params.put("SecOCKey", user_key)
      except Exception:
        pass

      secoc_key = self.params.get("SecOCKey")
      if secoc_key is not None:
        saved_secoc_key = bytes.fromhex(secoc_key.strip())
        if len(saved_secoc_key) == 16:
          self.CP.secOcKeyAvailable = True
          self.CI.CS.secoc_key = saved_secoc_key
          if controller_available:
            self.CI.CC.secoc_key = saved_secoc_key
        else:
          cloudlog.warning("Saved SecOC key is invalid")

    # Write previous route's CarParams
    prev_cp = self.params.get("CarParamsPersistent")
    if prev_cp is not None:
      self.params.put("CarParamsPrevRoute", prev_cp)

    # Write CarParams for controls and radard
    cp_bytes = self.CP.to_bytes()
    self.params.put("CarParams", cp_bytes)
    self.params.put_nonblocking("CarParamsCache", cp_bytes)
    self.params.put_nonblocking("CarParamsPersistent", cp_bytes)

    self.mock_carstate = MockCarState()
    self.v_cruise_helper = VCruiseHelper(self.CP, self.FPCP)
    self.redneck_cruise = RedneckCruise(self.CP, self.FPCP) if self.CP.brand == "hyundai" and \
      self.FPCP.redneckCruiseAvailable and not self.FPCP.pcmCruiseSpeed else None
    EV9DashCoordinator.initialize(self)

    self.is_metric = self.params.get_bool("IsMetric")
    self.safe_mode = self.params.get_bool("SafeMode")
    self.experimental_mode = self.params.get_bool("ExperimentalMode") and not self.safe_mode

    # card is driven by can recv, expected at 100Hz
    self.rk = Ratekeeper(100, print_delay_threshold=None)

    self.resume_prev_button = False

    self.starpilot_toggles = get_starpilot_toggles()

    self.FPCP.alternativeExperience |= interface_alternative_experience

    if self.starpilot_toggles.always_on_lateral:
      self.CP.alternativeExperience |= ALTERNATIVE_EXPERIENCE.ALWAYS_ON_LATERAL
      self.FPCP.alternativeExperience |= ALTERNATIVE_EXPERIENCE.ALWAYS_ON_LATERAL
    if getattr(self.starpilot_toggles, "remap_cancel_to_distance", False):
      self.CP.alternativeExperience |= ALTERNATIVE_EXPERIENCE.GM_REMAP_CANCEL_TO_DISTANCE
      self.FPCP.alternativeExperience |= ALTERNATIVE_EXPERIENCE.GM_REMAP_CANCEL_TO_DISTANCE

    fpcp_bytes = self.FPCP.to_bytes()
    self.params.put("StarPilotCarParams", fpcp_bytes)
    self.params.put_nonblocking("StarPilotCarParamsPersistent", fpcp_bytes)

    # OFF -> READY can put the EV9 ADAS ECU into a state that rejects
    # CommunicationControl long before the rest of selfdrive is initialized.
    # The production EV9 longitudinal profile suppresses the ECU immediately
    # after fingerprinting and begins the inactive replacement set while
    # selfdrive finishes starting. This avoids a second knockout at the normal
    # controls-ready handoff.
    ev9_early_requested = bool(not self.CP.passive and str(self.CP.carFingerprint) == "KIA_EV9" and
                               self.CP.openpilotLongitudinalControl)
    if ev9_early_requested:
      cloudlog.warning("EV9 production early interface initialization requested")
      refreshed_handoff = ev9_panda_handoff
      if self.params.get_bool("EV9LongPreinitPanda"):
        refreshed_handoff = revalidate_ev9_panda_preinit_handoff(self.sm)
        if not refreshed_handoff.adoptable:
          cloudlog.error(f"EV9 Panda preinit changed before host takeover: {refreshed_handoff.reason}")
      self._initialize_car_interface(signal_controls_ready=False)
      self.ev9_early_control_active = self.CP.openpilotLongitudinalControl and not self.params.get_bool("EcuDisableFailed")
      panda_preinit_takeover = ev9_preinit_refreshed_takeover_allowed(
        ev9_panda_preinit_armed(self.params), refreshed_handoff, self.ev9_early_control_active,
      )
      self.params.put_bool_nonblocking("ControlsReady", True)
      if self.ev9_early_control_active and panda_preinit_takeover:
        # `sendcan.valid=False` is metadata, not a transport gate. Do not call
        # CI.apply at all until pandad has installed final Hyundai safety and a
        # complete latest resident counter/body snapshot is available.
        self._prepare_ev9_panda_takeover()
      elif self.ev9_early_control_active:
        self._send_ev9_early_inactive_reconstruction(valid=False)
        cloudlog.warning("EV9 legacy early inactive reconstruction primed")
      cloudlog.warning(f"EV9 early inactive reconstruction active={self.ev9_early_control_active}")

    update_starpilot_toggles()

    self.starpilot_card = StarPilotCard(self.CP, self.FPCP)

    extra_services = ['starpilotOnroadEvents', 'starpilotPlan', 'starpilotSelfdriveState', 'liveCalibration', 'selfdriveState']
    if str(self.CP.carFingerprint) == "KIA_EV9":
      extra_services.append('modelV2')
    self.sm = self.sm.extend(extra_services)
    self.pm = self.pm.extend(['starpilotCarState'])

  def _inject_favorite_virtual_cruise_events(self, CS: car.CarState) -> None:
    virtual_events = [
      structs.CarState.ButtonEvent(pressed=False, type=button_type)
      for button_type in self._favorite_virtual_releases
    ]
    self._favorite_virtual_releases = []

    for counter_key, counter_attr, button_type in (
      (FAVORITE_ACTION_ACCEL_COUNTER, "_favorite_virtual_accel_counter", ButtonType.accelCruise),
      (FAVORITE_ACTION_DECEL_COUNTER, "_favorite_virtual_decel_counter", ButtonType.decelCruise),
    ):
      counter = self.params_memory.get_int(counter_key)
      if counter == getattr(self, counter_attr):
        continue
      setattr(self, counter_attr, counter)
      virtual_events.append(structs.CarState.ButtonEvent(pressed=True, type=button_type))
      self._favorite_virtual_releases.append(button_type)

    if virtual_events:
      CS.buttonEvents = list(CS.buttonEvents) + virtual_events

  def _initialize_car_interface(self, signal_controls_ready: bool = True) -> None:
    if self.interface_initialized:
      return

    was_openpilot_long = self.CP.openpilotLongitudinalControl
    self.CI.init(self.CP, *self.can_callbacks)
    # If ECU disable was skipped/failed, strip LONG safety from both parameter
    # sets before ControlsReady lets pandad select the vehicle safety model.
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
    if signal_controls_ready:
      self.params.put_bool_nonblocking("ControlsReady", True)

  def _send_ev9_early_inactive_reconstruction(self, valid: bool) -> bool:
    return EV9PreinitCoordinator._send_ev9_early_inactive_reconstruction(self, valid)

  def _send_ev9_panda_claim_retries(self) -> bool:
    return EV9PreinitCoordinator._send_ev9_panda_claim_retries(self)

  @staticmethod
  def _resident_ev9_status(panda_states):
    return EV9PreinitCoordinator._resident_ev9_status(panda_states)

  def _expected_ev9_panda_safety(self):
    return EV9PreinitCoordinator._expected_ev9_panda_safety(self)

  def _drain_ev9_preinit_can(self, baselines: dict | None = None, claim_receipts: set | None = None) -> None:
    EV9PreinitCoordinator._drain_ev9_preinit_can(self, baselines, claim_receipts)

  def _fault_ev9_panda_takeover(self, reason: str) -> None:
    EV9PreinitCoordinator._fault_ev9_panda_takeover(self, reason)

  def _enter_ev9_panda_off(self, handoff, terminal_ignition_on: bool | None, now: float) -> None:
    EV9PreinitCoordinator._enter_ev9_panda_off(self, handoff, terminal_ignition_on, now)

  def _run_ev9_panda_claim(self, timeout_s: float, resume_fresh_can: bool = False) -> bool:
    return EV9PreinitCoordinator._run_ev9_panda_claim(self, timeout_s, resume_fresh_can)

  def _prepare_ev9_panda_takeover(self) -> None:
    EV9PreinitCoordinator._prepare_ev9_panda_takeover(self)

  def _update_ev9_panda_takeover(self) -> None:
    EV9PreinitCoordinator._update_ev9_panda_takeover(self)

  def state_update(self) -> tuple[car.CarState, structs.RadarDataT | None]:
    """carState update loop, driven by can"""

    # Keep the blocking CAN scheduler. Panda's bridge and the surviving vehicle
    # streams remain live; a nonblocking drain would bypass monitor_time's
    # pacing and flood sendcan during startup.
    can_strs = messaging.drain_sock_raw(self.can_sock, wait_for_one=True)
    can_list = can_capnp_to_list(can_strs)
    if self.ev9_preinit_takeover_state == EV9PreinitTakeoverState.CLAIMING:
      for _, frames in can_list:
        collect_ev9_preinit_claim_receipts(
          self.ev9_preinit_claim_receipts,
          [CanData(address, dat, src) for address, dat, src in frames],
        )

    # Update carState from CAN
    CS, FPCS = self.CI.update(can_list, self.starpilot_toggles)
    if self.CP.brand == 'mock':
      CS, FPCS = self.mock_carstate.update(CS, FPCS)
    self._inject_favorite_virtual_cruise_events(CS)

    # Update radar tracks from CAN
    RD: structs.RadarDataT | None = self.RI.update(can_list)

    self.sm.update(0)
    self._update_ev9_panda_takeover()
    # A resident-takeover failure is not evidence that the remaining vehicle
    # CAN parsers are invalid. Preserve parser truth and report the ADAS loss
    # explicitly so selfdrived can show the correct recovery action instead of
    # the misleading "Unknown Vehicle Variant" alert.
    CS.adasUnavailable = self.ev9_preinit_takeover_state == EV9PreinitTakeoverState.FAULTED

    self._update_ev9_raw_blindspot_gate(CS, RD)
    self._update_ev9_dash_tracker(CS, RD)
    radar_valid = self.sm.seen['radarState'] and self.sm.alive['radarState'] and self.sm.valid['radarState']
    self.CI.CS.openpilot_radar_valid = radar_valid
    # Historical fault recovery may be sufficient to finish a neutral handoff,
    # but it must never authorize actuation. Any current or latched Panda fault
    # is an unconditional actuation inhibit.
    if str(self.CP.carFingerprint) == "KIA_EV9":
      self.ev9_preinit_fault_history.update(self.sm['pandaStates'])
      panda_faulted = ev9_panda_faulted_for_actuation(self.sm['pandaStates'], self.sm.seen['pandaStates'])
      self.CI.CS.panda_faulted = panda_faulted or self.ev9_preinit_safety_quarantine
      CS.adasUnavailable = bool(self.CP.openpilotLongitudinalControl and
                                (self.ev9_preinit_takeover_state == EV9PreinitTakeoverState.FAULTED or panda_faulted))

    can_rcv_valid = len(can_strs) > 0

    # Check for CAN timeout
    if not can_rcv_valid:
      self.can_rcv_cum_timeout_counter += 1

    if can_rcv_valid and REPLAY:
      self.can_log_mono_time = messaging.log_from_bytes(can_strs[0]).logMonoTime

    preap_software_cruise = (
      self.CP.brand == "tesla" and self.CP.carFingerprint == "TESLA_MODEL_S_PREAP" and
      self.CP.openpilotLongitudinalControl and not self.CP.pcmCruise
    )
    if not preap_software_cruise:
      speed_limit_confirmation_pending = is_speed_limit_confirmation_pending(self.sm['starpilotPlan'])
      self.v_cruise_helper.update_v_cruise(
        CS,
        self.sm['carControl'].enabled,
        self.is_metric,
        speed_limit_confirmation_pending,
        self.starpilot_toggles,
        FPCS,
      )
    else:
      preap_v_cruise_kph = float(CS.cruiseState.speed * CV.MS_TO_KPH)
      self.v_cruise_helper.v_cruise_kph_last = self.v_cruise_helper.v_cruise_kph
      self.v_cruise_helper.v_cruise_kph = preap_v_cruise_kph
      self.v_cruise_helper.v_cruise_cluster_kph = preap_v_cruise_kph
    slc_force_speed = self.params_memory.get_float("SLCForceCruiseSpeed")
    if slc_force_speed > 0:
      if self.is_metric:
        new_cruise_kph = round(slc_force_speed * CV.MS_TO_KPH)
      else:
        new_cruise_kph = round(slc_force_speed * CV.MS_TO_MPH) * IMPERIAL_INCREMENT
      self.v_cruise_helper.v_cruise_kph = max(min(new_cruise_kph, V_CRUISE_MAX), V_CRUISE_MIN)
      self.v_cruise_helper.v_cruise_cluster_kph = self.v_cruise_helper.v_cruise_kph
      self.params_memory.remove("SLCForceCruiseSpeed")

    if self.sm['carControl'].enabled and not self.CC_prev.enabled and not preap_software_cruise:
      # Use CarState w/ buttons from the step selfdrived enables on
      desired_speed_limit = self.sm['starpilotPlan'].slcSpeedLimit + self.sm['starpilotPlan'].slcSpeedLimitOffset
      self.v_cruise_helper.initialize_v_cruise(
        self.CS_prev,
        self.experimental_mode,
        self.resume_prev_button,
        self.starpilot_toggles,
        desired_speed_limit=desired_speed_limit,
      )

    # TODO: mirror the carState.cruiseState struct?
    CS.vCruise = float(self.v_cruise_helper.v_cruise_kph)
    CS.vCruiseCluster = float(self.v_cruise_helper.v_cruise_cluster_kph)

    if any(be.type in (ButtonType.accelCruise, ButtonType.resumeCruise) for be in CS.buttonEvents):
      self.resume_prev_button = True
    elif any(be.type in (ButtonType.decelCruise, ButtonType.setCruise) for be in CS.buttonEvents):
      self.resume_prev_button = False

    FPCS = self.starpilot_card.update(CS, FPCS, self.sm, self.starpilot_toggles)
    if str(self.CP.carFingerprint) == "KIA_EV9":
      # Preserve AOL's user-visible availability across EV9's normal temporary
      # angle-steering lockout. controlsd still drops latActive and the
      # controller still sends an inactive measured-angle command; this flag is
      # display-only so reconstruction can keep the grey wheel visible instead
      # of incorrectly hiding the feature while the driver overrides it.
      self.CI.CS.ev9_always_on_lateral_enabled = bool(FPCS.alwaysOnLateralEnabled)

    return CS, RD, FPCS

  def _update_ev9_dash_tracker(self, CS: car.CarState, RD: structs.RadarDataT | None) -> None:
    EV9DashCoordinator._update_ev9_dash_tracker(self, CS, RD)

  def _update_ev9_raw_blindspot_gate(self, CS: car.CarState, RD: structs.RadarDataT | None) -> None:
    EV9DashCoordinator._update_ev9_raw_blindspot_gate(self, CS, RD)

  def state_publish(self, CS: car.CarState, RD: structs.RadarDataT | None, FPCS: custom.StarPilotCarState):
    """carState and carParams publish loop"""

    # carParams - logged every 50 seconds (> 1 per segment)
    if self.sm.frame % int(50. / DT_CTRL) == 0:
      cp_send = messaging.new_message('carParams')
      cp_send.valid = True
      cp_send.carParams = self.CP
      self.pm.send('carParams', cp_send)

    # publish new carOutput
    co_send = messaging.new_message('carOutput')
    co_send.valid = self.sm.all_checks(['carControl'])
    co_send.carOutput.actuatorsOutput = self.last_actuators_output
    self.pm.send('carOutput', co_send)

    # kick off controlsd step while we actuate the latest carControl packet
    cs_send = messaging.new_message('carState')
    cs_send.valid = CS.canValid
    cs_send.carState = CS
    cs_send.carState.canErrorCounter = self.can_rcv_cum_timeout_counter
    cs_send.carState.cumLagMs = -self.rk.remaining * 1000.
    self.pm.send('carState', cs_send)

    if RD is not None:
      tracks_msg = messaging.new_message('liveTracks')
      tracks_msg.valid = not any(RD.errors.to_dict().values())
      tracks_msg.liveTracks = RD
      self.pm.send('liveTracks', tracks_msg)

    fpcs_send = messaging.new_message('starpilotCarState')
    fpcs_send.valid = CS.canValid
    fpcs_send.starpilotCarState = FPCS
    self.pm.send('starpilotCarState', fpcs_send)

  def controls_update(self, CS: car.CarState, CC: car.CarControl):
    """control update loop, driven by carControl"""

    if not self.initialized_prev:
      # Initialize CarInterface, once controls are ready
      # TODO: this can make us miss at least a few cycles when doing an ECU knockout
      self._initialize_car_interface()

    if self.sm.all_alive(['carControl']):
      # send car controls over can
      now_nanos = self.can_log_mono_time if REPLAY else int(time.monotonic() * 1e9)
      self._update_redneck_cruise(CS, CC)
      self._update_openpilot_lead_state(CC)
      self._update_ev9_dash_scene(CS, CC)
      self.last_actuators_output, can_sends = self.CI.apply(CC, now_nanos, self.starpilot_toggles)
      self.pm.send('sendcan', can_list_to_can_capnp(can_sends, msgtype='sendcan', valid=CS.canValid))

      self.CC_prev = CC

  def _update_openpilot_lead_state(self, CC: car.CarControl) -> None:
    lead_visible = bool(CC.hudControl.leadVisible)
    lead_distance = 0.0
    lead_rel_speed = 0.0

    if self.sm.seen['radarState'] and self.sm.alive['radarState'] and self.sm.valid['radarState']:
      lead = self.sm['radarState'].leadOne
      if lead.status:
        lead_visible = True
        lead_distance = max(float(lead.dRel), 0.0)
        lead_rel_speed = float(lead.vRel)

    if lead_distance <= OPENPILOT_LEAD_MIN_DISTANCE:
      lead_distance = 0.0
      lead_rel_speed = 0.0

    self.CI.CS.openpilot_lead_visible = lead_visible
    self.CI.CS.openpilot_lead_distance = lead_distance
    self.CI.CS.openpilot_lead_rel_speed = lead_rel_speed

  def _update_ev9_dash_scene(self, CS: car.CarState, CC: car.CarControl) -> None:
    EV9DashCoordinator._update_ev9_dash_scene(self, CS, CC)

  def _update_redneck_cruise(self, CS: car.CarState, CC: car.CarControl) -> None:
    if self.redneck_cruise is None:
      return

    v_target_ms, lead_present = self._get_redneck_target_speed(CS)
    send_button, v_target = self.redneck_cruise.run(CS, CC, v_target_ms, self.is_metric, lead_present=lead_present)
    self.CI.CS.redneck_send_button = send_button
    self.CI.CS.redneck_v_target = v_target

  def _get_redneck_target_speed(self, CS: car.CarState) -> tuple[float, bool]:
    starpilot_target_speed = 0.0
    allow_plan_decrease = False
    lead_present = False
    lead_distance_m = 0.0
    lead_rel_speed_ms = 0.0
    lookahead_points = REDNECK_DECREASE_LOOKAHEAD_POINTS
    if self.sm.seen['starpilotPlan'] and self.sm.valid['starpilotPlan']:
      starpilot_target_speed = float(self.sm['starpilotPlan'].vCruise)

    plan_speeds = []
    if self.sm.seen['longitudinalPlan'] and self.sm.valid['longitudinalPlan']:
      longitudinal_plan = self.sm['longitudinalPlan']
      plan_speeds = [float(speed) for speed in longitudinal_plan.speeds if math.isfinite(float(speed))]
      lead_present = bool(longitudinal_plan.hasLead)
      allow_plan_decrease = bool(lead_present or longitudinal_plan.shouldStop or
                                 str(longitudinal_plan.longitudinalPlanSource) != "cruise")
      if lead_present and len(plan_speeds) > 0:
        lookahead_points = len(plan_speeds)
        if self.sm.seen['radarState'] and self.sm.valid['radarState']:
          lead = self.sm['radarState'].leadOne
          if lead.status:
            lead_distance_m = max(float(lead.dRel), 0.0)
            lead_rel_speed_ms = float(lead.vRel)

    return select_redneck_target_speed(
      float(getattr(CS, "vCruise", 0.0)),
      float(CS.cruiseState.speedCluster),
      starpilot_target_speed,
      plan_speeds,
      lookahead_points,
      allow_plan_decrease=allow_plan_decrease,
      lead_present=lead_present,
      lead_distance_m=lead_distance_m,
      lead_rel_speed_ms=lead_rel_speed_ms,
    ), lead_present

  def step(self):
    CS, RD, FPCS = self.state_update()
    resume_fresh_can = self.ev9_preinit_resume_fresh_can
    self.ev9_preinit_resume_fresh_can = False

    self.state_publish(CS, RD, FPCS)

    initialized = (not any(e.name == EventName.selfdriveInitializing for e in self.sm['onroadEvents']) and
                   self.sm.seen['onroadEvents'])
    if resume_fresh_can or self.ev9_preinit_takeover_state in (EV9PreinitTakeoverState.FAULTED,
                                                               EV9PreinitTakeoverState.OFF,
                                                               EV9PreinitTakeoverState.WAIT_SAFETY):
      pass
    elif self.ev9_preinit_takeover_state == EV9PreinitTakeoverState.CLAIMING:
      self._send_ev9_early_inactive_reconstruction(CS.canValid)
    elif not self.CP.passive and initialized:
      self.controls_update(CS, self.sm['carControl'])
    elif self.ev9_early_control_active:
      self._send_ev9_early_inactive_reconstruction(CS.canValid)

    self.initialized_prev = initialized
    self.CS_prev = CS

    self.CI.CS.CC = self.sm['carControl']

    self.starpilot_toggles = get_starpilot_toggles(self.sm)

  def params_thread(self, evt):
    while not evt.is_set():
      self.safe_mode = self.params.get_bool("SafeMode")
      self.is_metric = self.params.get_bool("IsMetric")
      self.experimental_mode = self.params.get_bool("ExperimentalMode") and self.CP.openpilotLongitudinalControl and not self.safe_mode
      EV9DashCoordinator.refresh_settings(self)
      time.sleep(0.1)

  def card_thread(self):
    e = threading.Event()
    t = threading.Thread(target=self.params_thread, args=(e, ))
    try:
      t.start()
      while True:
        self.step()
        self.rk.monitor_time()
    finally:
      e.set()
      t.join()


def main():
  config_realtime_process(4, Priority.CTRL_HIGH)
  car = Car()
  car.card_thread()


if __name__ == "__main__":
  main()
