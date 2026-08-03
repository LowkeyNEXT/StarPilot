from dataclasses import dataclass
from enum import IntEnum, IntFlag

from opendbc.car import CanData, structs
from opendbc.car.disable_ecu import disable_ecu, ecu_log
from opendbc.car.hyundai import ev9_canfd
from opendbc.car.hyundai.hyundaicanfd import CanBus
from opendbc.car.hyundai.values import CAR, HyundaiFlags


Ecu = structs.CarParams.Ecu

EV9_EARLY_SUPPRESSION_ACTIVE = False
EV9_PANDA_PREINIT_STATUS_VERSION = 4
EV9_COMMUNICATION_CONTROL_REQUEST = b"\x28\x01\x01"
EV9_COMMUNICATION_CONTROL_RESTORE = b"\x28\x00\x01"
EV9_PRODUCTION_SAFETY_PARAM = 0x8495
EV9_OPTIONAL_SAFETY_PARAM = 0x800


class EV9PandaPreinitState(IntEnum):
  COLLECTING = 0
  WAIT_SESSION = 1
  WAIT_COMM_CONTROL = 2
  WAIT_SUPPRESSION = 3
  ACTIVE = 4
  HANDOFF = 5
  ABORTED = 6
  RESTORING = 7
  READY_PENDING_RESPONSE = 8


class EV9PandaPreinitFlags(IntFlag):
  IDENTITY_VALID = 0x01
  START_INTENT = 0x02
  SUPPRESSION_CONFIRMED = 0x04
  BRIDGE_ACTIVE = 0x08
  HOST_HANDOFF = 0x10
  DEADLINE_MISSED = 0x20
  RESTORE_SENT = 0x40
  INTERNAL_TX_REJECTED = 0x80


class EV9PandaPreinitOwner(IntEnum):
  NONE = 0
  PANDA_PENDING = 1
  PANDA = 2
  HOST = 3
  FAILED = 4


@dataclass(frozen=True)
class EV9PandaPreinitHandoff:
  owner: EV9PandaPreinitOwner = EV9PandaPreinitOwner.NONE
  state: int = -1
  flags: EV9PandaPreinitFlags = EV9PandaPreinitFlags(0)
  resident: bool = False
  sample_valid: bool = False
  reason: str = "no live Panda preinit status"

  @property
  def knockout_owned(self) -> bool:
    return self.owner in (EV9PandaPreinitOwner.PANDA_PENDING, EV9PandaPreinitOwner.PANDA,
                          EV9PandaPreinitOwner.HOST)

  @property
  def adoptable(self) -> bool:
    return self.owner in (EV9PandaPreinitOwner.PANDA, EV9PandaPreinitOwner.HOST)

  @property
  def host_uds_veto(self) -> bool:
    return self.resident or self.knockout_owned


EV9_PANDA_PREINIT_HANDOFF = EV9PandaPreinitHandoff()


@dataclass(frozen=True)
class EV9InterfaceInitProfile:
  enabled: bool = False
  communication_control: bytes | None = None
  handoff_adopted: bool = False
  host_request_veto: bool = False
  handoff_reason: str = "not an EV9 longitudinal initialization"


def _ev9_preinit_statuses(panda_states) -> list:
  statuses = []
  for panda_state in panda_states or ():
    status = getattr(panda_state, "ev9LongPreinitStatus", None)
    if status is not None and (bool(getattr(status, "resident", False)) or
                               bool(getattr(status, "valid", False))):
      statuses.append(status)
  return statuses


def update_ev9_panda_preinit_handoff(panda_states) -> EV9PandaPreinitHandoff:
  """Consume the current, non-persistent PandaState ownership proof."""
  global EV9_PANDA_PREINIT_HANDOFF
  statuses = _ev9_preinit_statuses(panda_states)
  if len(statuses) == 0:
    EV9_PANDA_PREINIT_HANDOFF = EV9PandaPreinitHandoff()
    return EV9_PANDA_PREINIT_HANDOFF
  if len(statuses) != 1:
    EV9_PANDA_PREINIT_HANDOFF = EV9PandaPreinitHandoff(
      owner=EV9PandaPreinitOwner.FAILED, resident=True,
      reason="ambiguous resident status from multiple Pandas",
    )
    return EV9_PANDA_PREINIT_HANDOFF

  status = statuses[0]
  resident = bool(getattr(status, "resident", False)) or bool(getattr(status, "valid", False))
  sample_valid = bool(getattr(status, "valid", False))
  if not sample_valid:
    EV9_PANDA_PREINIT_HANDOFF = EV9PandaPreinitHandoff(
      owner=EV9PandaPreinitOwner.FAILED, resident=resident,
      reason="resident Panda preinit status read was invalid",
    )
    return EV9_PANDA_PREINIT_HANDOFF

  version = int(getattr(status, "version", -1))
  state_value = int(getattr(status, "state", -1))
  flags = EV9PandaPreinitFlags(int(getattr(status, "flags", 0)))
  communication_type = int(getattr(status, "communicationType", -1))
  timing_valid = bool(getattr(status, "timingValid", False))
  try:
    state = EV9PandaPreinitState(state_value)
  except ValueError:
    state = None
  if version != EV9_PANDA_PREINIT_STATUS_VERSION:
    owner, reason = EV9PandaPreinitOwner.FAILED, f"unsupported status version {version}"
  elif communication_type != 0x01:
    owner, reason = EV9PandaPreinitOwner.FAILED, f"unexpected communication type 0x{communication_type:02x}"
  elif not timing_valid:
    owner, reason = EV9PandaPreinitOwner.PANDA_PENDING, "Panda timing proof is temporarily unavailable"
  elif state == EV9PandaPreinitState.RESTORING:
    owner, reason = EV9PandaPreinitOwner.PANDA_PENDING, "Panda is restoring ECU communication"
  elif flags & (EV9PandaPreinitFlags.DEADLINE_MISSED | EV9PandaPreinitFlags.INTERNAL_TX_REJECTED):
    owner, reason = EV9PandaPreinitOwner.FAILED, "Panda missed a bridge deadline or rejected an internal TX"
  else:
    clean_common = bool(flags & EV9PandaPreinitFlags.IDENTITY_VALID) and \
      bool(flags & EV9PandaPreinitFlags.SUPPRESSION_CONFIRMED)
    if state == EV9PandaPreinitState.ACTIVE and clean_common and flags & EV9PandaPreinitFlags.BRIDGE_ACTIVE:
      owner, reason = EV9PandaPreinitOwner.PANDA, "Panda owns confirmed suppression and bridge"
    elif state == EV9PandaPreinitState.HANDOFF and clean_common and \
        flags & EV9PandaPreinitFlags.BRIDGE_ACTIVE and flags & EV9PandaPreinitFlags.HOST_HANDOFF:
      owner, reason = EV9PandaPreinitOwner.HOST, "host handoff already confirmed"
    elif state == EV9PandaPreinitState.ABORTED:
      owner, reason = EV9PandaPreinitOwner.FAILED, "Panda preinit ended in ABORTED"
    elif state in (EV9PandaPreinitState.COLLECTING, EV9PandaPreinitState.WAIT_SESSION,
                   EV9PandaPreinitState.WAIT_COMM_CONTROL, EV9PandaPreinitState.WAIT_SUPPRESSION,
                   EV9PandaPreinitState.READY_PENDING_RESPONSE):
      owner, reason = EV9PandaPreinitOwner.PANDA_PENDING, f"Panda transaction is still {state.name}"
    else:
      owner, reason = EV9PandaPreinitOwner.FAILED, "inconsistent Panda preinit ownership proof"

  EV9_PANDA_PREINIT_HANDOFF = EV9PandaPreinitHandoff(
    owner=owner, state=state_value, flags=flags, resident=resident, sample_valid=True, reason=reason,
  )
  return EV9_PANDA_PREINIT_HANDOFF


def invalidate_ev9_panda_preinit_handoff(reason: str) -> EV9PandaPreinitHandoff:
  global EV9_PANDA_PREINIT_HANDOFF
  EV9_PANDA_PREINIT_HANDOFF = EV9PandaPreinitHandoff(
    owner=EV9PandaPreinitOwner.PANDA_PENDING,
    resident=EV9_PANDA_PREINIT_HANDOFF.resident,
    reason=reason,
  )
  return EV9_PANDA_PREINIT_HANDOFF


def ev9_panda_preinit_baselines(messages: list) -> list:
  return [CanData(msg.address, msg.dat, msg.src - 0x80 if 0x80 <= msg.src < 0xC0 else msg.src) for msg in messages]


def ev9_panda_preinit_armed(params) -> bool:
  """Return the explicit production gate shared by pandad, card, and the interface."""
  try:
    return (params.get_bool("EV9LongPreinitPanda") and
            params.get_bool("OpenpilotEnabledToggle") and
            params.get_bool("AlphaLongitudinalEnabled"))
  except Exception:
    return False


def ev9_interface_init_profile(CP, params, normal_init: bool) -> EV9InterfaceInitProfile:
  """Resolve EV9 ownership once so Hyundai's generic init keeps a narrow hook."""
  global EV9_EARLY_SUPPRESSION_ACTIVE
  enabled = CP.carFingerprint == CAR.KIA_EV9 and CP.openpilotLongitudinalControl
  if not enabled:
    return EV9InterfaceInitProfile()

  preinit_requested = params.get_bool("EV9LongPreinitPanda")
  preinit_armed = ev9_panda_preinit_armed(params)
  handoff = EV9_PANDA_PREINIT_HANDOFF
  resume_early = normal_init and EV9_EARLY_SUPPRESSION_ACTIVE and not preinit_requested and not handoff.host_uds_veto
  resume_panda = normal_init and preinit_armed and handoff.adoptable
  transaction_owned = normal_init and handoff.host_uds_veto
  EV9_EARLY_SUPPRESSION_ACTIVE = False
  adopted = resume_early or resume_panda
  return EV9InterfaceInitProfile(
    enabled=True,
    communication_control=EV9_COMMUNICATION_CONTROL_REQUEST if normal_init else None,
    handoff_adopted=adopted,
    host_request_veto=not adopted and normal_init and (preinit_requested or transaction_owned),
    handoff_reason=handoff.reason,
  )


def ev9_cached_safety_profile_supported(cached_params) -> bool:
  """Validate the persisted host profile before any pre-fingerprint UDS TX."""
  try:
    config = cached_params.safetyConfigs[-1]
    required_flags = (HyundaiFlags.CANFD | HyundaiFlags.EV | HyundaiFlags.CANFD_LKA_STEERING |
                      HyundaiFlags.CANFD_LKA_STEERING_ALT | HyundaiFlags.CANFD_ANGLE_STEERING)
    return (config.safetyModel == structs.CarParams.SafetyModel.hyundaiCanfd and
            (int(config.safetyParam) & ~EV9_OPTIONAL_SAFETY_PARAM) == EV9_PRODUCTION_SAFETY_PARAM and
            (int(cached_params.flags) & int(required_flags)) == int(required_flags))
  except (AttributeError, IndexError, TypeError):
    return False


def attempt_ev9_pre_fingerprint_suppression(cached_params, params, can_recv=None, can_send=None,
                                            initial_can_messages: list | None = None) -> bool:
  """Adopt Panda ownership or attempt the legacy host knockout before fingerprinting."""
  global EV9_EARLY_SUPPRESSION_ACTIVE
  EV9_EARLY_SUPPRESSION_ACTIVE = False
  ev9_canfd.set_adrv_baselines([])
  if cached_params is None or cached_params.carFingerprint != CAR.KIA_EV9 or cached_params.brand != "hyundai":
    return False
  if not params.get_bool("AlphaLongitudinalEnabled") or not cached_params.openpilotLongitudinalControl or cached_params.pcmCruise:
    return False
  if not ev9_cached_safety_profile_supported(cached_params):
    return False

  has_adas_fw = any(fw.ecu == Ecu.adas and fw.address == 0x730 for fw in cached_params.carFw)
  if not has_adas_fw:
    return False

  observed_can_messages = list(initial_can_messages or [])
  handoff = EV9_PANDA_PREINIT_HANDOFF
  if params.get_bool("EV9LongPreinitPanda") or handoff.host_uds_veto:
    if ev9_panda_preinit_armed(params) and handoff.adoptable:
      EV9_EARLY_SUPPRESSION_ACTIVE = True
      ev9_canfd.set_adrv_baselines(ev9_panda_preinit_baselines(observed_can_messages))
      ecu_log(f"=== EV9 PANDA PREINIT HANDOFF accepted: {handoff.reason} ===")
      return True
    ecu_log(f"=== EV9 PANDA PREINIT HANDOFF rejected: {handoff.reason} ===")
    return False

  def observing_can_recv(wait_for_one=False):
    packets = can_recv(wait_for_one=wait_for_one)
    for packet in packets:
      observed_can_messages.extend(packet)
    return packets

  ecu_log("=== EV9 PRE-FINGERPRINT SUPPRESSION ATTEMPT ===")
  observed_recv = observing_can_recv if can_recv is not None else can_recv
  EV9_EARLY_SUPPRESSION_ACTIVE = disable_ecu(
    observed_recv, can_send, bus=CanBus(cached_params).ECAN, addr=0x730,
    com_cont_req=EV9_COMMUNICATION_CONTROL_REQUEST, session_delay=0.0, require_response=True,
  )
  if EV9_EARLY_SUPPRESSION_ACTIVE:
    normalized_messages = ev9_panda_preinit_baselines(observed_can_messages)
    ev9_canfd.set_adrv_baselines(normalized_messages)
    baseline_addresses = {0x160, 0x161, 0x162, 0x1A0, 0x1BA, 0x1DA, 0x1E0, 0x1E5, 0x1EA, 0x200, 0x345, 0x38C, 0x57A}
    observed_addresses = sorted({msg.address for msg in normalized_messages if msg.src == 1 and msg.address in baseline_addresses})
    ecu_log(f"=== EV9 READY BASELINES captured={[hex(address) for address in observed_addresses]} ===")
  else:
    ev9_canfd.set_adrv_baselines([])
  ecu_log(f"=== EV9 PRE-FINGERPRINT SUPPRESSION result={EV9_EARLY_SUPPRESSION_ACTIVE} ===")
  return EV9_EARLY_SUPPRESSION_ACTIVE
