#include "selfdrive/pandad/ev9_preinit.h"

#include <cstdlib>
#include <string>

std::optional<PandaEv9LongPreinitStatus> Panda::get_ev9_long_preinit_status() {
  static_assert(sizeof(ev9_long_preinit_status_t) == USBPACKET_MAX_SIZE);
  static_assert(sizeof(ev9_long_preinit_timing_t) == USBPACKET_MAX_SIZE);
  constexpr int legacy_status_size = 60;

  PandaEv9LongPreinitStatus result = {};
  const int status_size = handle->control_read(0xe9, EV9_LONG_PREINIT_STATUS_PAGE, 0,
                                               reinterpret_cast<unsigned char *>(&result.status),
                                               sizeof(result.status));
  const bool current_status = status_size == static_cast<int>(sizeof(result.status)) &&
                              result.status.version == EV9_LONG_PREINIT_STATUS_VERSION;
  const bool legacy_status = status_size == legacy_status_size && result.status.version == 3U;
  if (!current_status && !legacy_status) {
    return std::nullopt;
  }

  if (legacy_status) {
    result.status.flags = 0U;
    result.status.outcome_us = 0U;
  }

  if (current_status) {
    const ev9_long_preinit_status_t first_status = result.status;
    const int timing_size = handle->control_read(0xe9, EV9_LONG_PREINIT_TIMING_PAGE, 0,
                                                 reinterpret_cast<unsigned char *>(&result.timing),
                                                 sizeof(result.timing));
    const bool timing_read_valid = timing_size == static_cast<int>(sizeof(result.timing)) &&
                                   result.timing.version == EV9_LONG_PREINIT_STATUS_VERSION &&
                                   result.timing.page == EV9_LONG_PREINIT_TIMING_PAGE;
    if (timing_read_valid) {
      ev9_long_preinit_status_t verified_status = {};
      const int verified_size = handle->control_read(0xe9, EV9_LONG_PREINIT_STATUS_PAGE, 0,
                                                      reinterpret_cast<unsigned char *>(&verified_status),
                                                      sizeof(verified_status));
      const bool verified_valid = verified_size == static_cast<int>(sizeof(verified_status)) &&
                                  verified_status.version == EV9_LONG_PREINIT_STATUS_VERSION;
      if (verified_valid) {
        result.status = verified_status;
        result.timing_valid = first_status.state == verified_status.state &&
                              first_status.flags == verified_status.flags &&
                              first_status.attempts == verified_status.attempts &&
                              first_status.state_started_us == verified_status.state_started_us &&
                              first_status.comm_control_us == verified_status.comm_control_us &&
                              first_status.outcome_us == verified_status.outcome_us &&
                              result.timing.flags == verified_status.flags;
      }
    }
  }
  return result;
}

bool ev9_preinit_status_enabled(Panda *panda) {
  const char *configured_serials = getenv("BOARDD_EV9_LONG_PREINIT_SERIALS");
  if (configured_serials == nullptr) {
    return false;
  }

  const std::string serial_list = "," + std::string(configured_serials) + ",";
  return serial_list.find("," + panda->hw_serial() + ",") != std::string::npos;
}

void fill_ev9_long_preinit_status(cereal::PandaState::Ev9LongPreinitStatus::Builder &ps,
                                  const std::optional<PandaEv9LongPreinitStatus> &preinit_status,
                                  bool resident) {
  ps.setResident(resident);
  ps.setValid(false);
  if (!preinit_status) {
    return;
  }

  const auto &status = preinit_status->status;
  ps.setValid(true);
  ps.setVersion(status.version);
  ps.setState(status.state);
  ps.setFlags(status.flags);
  ps.setFingerprint(status.fingerprint);
  ps.setAttempts(status.attempts);
  ps.setLastService(status.last_service);
  ps.setLastResponse(status.last_response);
  ps.setLastNrc(status.last_nrc);
  ps.setCommunicationType(status.communication_type);
  ps.setTrigger(status.trigger);
  ps.setFirstEcanLen(status.first_ecan_len);
  ps.setPowertrainState(status.powertrain_state);
  ps.setPowertrainBootState(status.powertrain_boot_state);
  ps.setPowertrainInitState(status.powertrain_init_state);
  ps.setFirstEcanAddr(status.first_ecan_addr);
  ps.setFirstCanUs(status.first_can_us);
  ps.setStateStartedUs(status.state_started_us);
  ps.setTriggerUs(status.trigger_us);
  ps.setFirstEcanUs(status.first_ecan_us);
  ps.setDriverBrakingUs(status.driver_braking_us);
  ps.setPreReadyUs(status.pre_ready_us);
  ps.setIgnitionUs(status.ignition_us);
  ps.setSessionResponseUs(status.session_response_us);
  ps.setCommControlUs(status.comm_control_us);
  ps.setLastPowertrainUs(status.last_powertrain_us);
  ps.setReadyUs(status.ready_us);
  ps.setOutcomeUs(status.outcome_us);

  ps.setTimingValid(preinit_status->timing_valid);
  if (preinit_status->timing_valid) {
    const auto &timing = preinit_status->timing;
    ps.setTimingFlags(timing.flags);
    ps.setCycleStartedUs(timing.cycle_started_us);
    ps.setSessionRequestUs(timing.session_request_us);
    ps.setCommControlResponseUs(timing.comm_control_response_us);
    ps.setLastCriticalAdasUs(timing.last_critical_adas_us);
    ps.setFirstReplacementUs(timing.first_replacement_us);
    ps.setSuppressionConfirmedUs(timing.suppression_confirmed_us);
    ps.setHandoffUs(timing.handoff_us);
    ps.setRestoreUs(timing.restore_us);
    ps.setAbortUs(timing.abort_us);
    ps.setLastHostTxUs(timing.last_host_tx_us);
    ps.setLastTesterPresentUs(timing.last_tester_present_us);
    ps.setLastVehicleFrameUs(timing.last_vehicle_frame_us);
  }
}
