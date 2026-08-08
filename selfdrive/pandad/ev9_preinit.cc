#include "selfdrive/pandad/ev9_preinit.h"

#include <array>
#include <cstdint>
#include <cstring>
#include <cstdlib>
#include <string>

namespace {

struct __attribute__((packed)) Ev9PreinitV1Status {
  uint8_t version;
  uint8_t state;
  uint8_t fingerprint;
  uint8_t attempts;
  uint8_t last_service;
  uint8_t last_response;
  uint8_t last_nrc;
  uint8_t communication_type;
  uint32_t first_can_us;
  uint32_t state_started_us;
};

struct __attribute__((packed)) Ev9PreinitV2Status {
  uint8_t version;
  uint8_t state;
  uint8_t fingerprint;
  uint8_t attempts;
  uint8_t last_service;
  uint8_t last_response;
  uint8_t last_nrc;
  uint8_t communication_type;
  uint8_t trigger;
  uint8_t first_ecan_len;
  uint16_t first_ecan_addr;
  uint32_t first_can_us;
  uint32_t state_started_us;
  uint32_t trigger_us;
  uint32_t first_ecan_us;
  uint32_t driver_braking_us;
  uint32_t pre_ready_us;
  uint32_t ignition_us;
  uint32_t session_response_us;
  uint32_t comm_control_us;
};

static_assert(sizeof(Ev9PreinitV1Status) == 16U);
static_assert(sizeof(Ev9PreinitV2Status) == 48U);

}  // namespace

std::optional<PandaEv9LongPreinitStatus> Panda::get_ev9_long_preinit_status() {
  static_assert(sizeof(ev9_long_preinit_status_t) == USBPACKET_MAX_SIZE);
  static_assert(sizeof(ev9_long_preinit_timing_t) == USBPACKET_MAX_SIZE);
  constexpr int legacy_status_size = 60;

  PandaEv9LongPreinitStatus result = {};
  std::array<unsigned char, USBPACKET_MAX_SIZE> status_buffer = {};
  const int status_size = handle->control_read(0xe9, EV9_LONG_PREINIT_STATUS_PAGE, 0,
                                               status_buffer.data(), status_buffer.size());
  const uint8_t status_version = status_size > 0 ? status_buffer[0] : 0U;
  const bool current_status = status_size == static_cast<int>(sizeof(result.status)) &&
                              status_version == EV9_LONG_PREINIT_STATUS_VERSION;
  const bool v3_status = status_size == legacy_status_size && status_version == 3U;
  const bool v2_status = status_size == static_cast<int>(sizeof(Ev9PreinitV2Status)) && status_version == 2U;
  const bool v1_status = status_size == static_cast<int>(sizeof(Ev9PreinitV1Status)) && status_version == 1U;
  if (!current_status && !v3_status && !v2_status && !v1_status) {
    return std::nullopt;
  }

  if (current_status || v3_status) {
    std::memcpy(&result.status, status_buffer.data(), static_cast<size_t>(status_size));
  } else if (v2_status) {
    Ev9PreinitV2Status legacy = {};
    std::memcpy(&legacy, status_buffer.data(), sizeof(legacy));
    result.status.version = legacy.version;
    result.status.state = legacy.state;
    result.status.fingerprint = legacy.fingerprint;
    result.status.attempts = legacy.attempts;
    result.status.last_service = legacy.last_service;
    result.status.last_response = legacy.last_response;
    result.status.last_nrc = legacy.last_nrc;
    result.status.communication_type = legacy.communication_type;
    result.status.trigger = legacy.trigger;
    result.status.first_ecan_len = legacy.first_ecan_len;
    result.status.first_ecan_addr = legacy.first_ecan_addr;
    result.status.first_can_us = legacy.first_can_us;
    result.status.state_started_us = legacy.state_started_us;
    result.status.trigger_us = legacy.trigger_us;
    result.status.first_ecan_us = legacy.first_ecan_us;
    result.status.driver_braking_us = legacy.driver_braking_us;
    result.status.pre_ready_us = legacy.pre_ready_us;
    result.status.ignition_us = legacy.ignition_us;
    result.status.session_response_us = legacy.session_response_us;
    result.status.comm_control_us = legacy.comm_control_us;
  } else {
    Ev9PreinitV1Status legacy = {};
    std::memcpy(&legacy, status_buffer.data(), sizeof(legacy));
    result.status.version = legacy.version;
    result.status.state = legacy.state;
    result.status.fingerprint = legacy.fingerprint;
    result.status.attempts = legacy.attempts;
    result.status.last_service = legacy.last_service;
    result.status.last_response = legacy.last_response;
    result.status.last_nrc = legacy.last_nrc;
    result.status.communication_type = legacy.communication_type;
    result.status.first_can_us = legacy.first_can_us;
    result.status.state_started_us = legacy.state_started_us;
  }

  if (!current_status) {
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

bool Panda::request_ev9_long_preinit_offroad_rearm(uint32_t cycle_started_us) {
  unsigned char accepted = 0U;
  const int response_size = handle->control_read(
    EV9_LONG_PREINIT_CONTROL_REQUEST, EV9_LONG_PREINIT_REARM_OFFROAD,
    static_cast<uint16_t>(cycle_started_us), &accepted, sizeof(accepted));
  return (response_size == static_cast<int>(sizeof(accepted))) && (accepted == 1U);
}

bool ev9_preinit_status_enabled(Panda *panda) {
  const char *configured_serials = getenv("BOARDD_EV9_LONG_PREINIT_SERIALS");
  if (configured_serials == nullptr) {
    return false;
  }

  const std::string serial_list = "," + std::string(configured_serials) + ",";
  return serial_list.find("," + panda->hw_serial() + ",") != std::string::npos;
}

bool ev9_preinit_maybe_rearm_offroad(
    Panda *panda, const std::optional<PandaEv9LongPreinitStatus> &preinit_status,
    bool is_onroad, bool ignition) {
  if (is_onroad || ignition || !preinit_status || !preinit_status->timing_valid ||
      preinit_status->status.version != EV9_LONG_PREINIT_STATUS_VERSION) {
    return false;
  }

  const uint8_t state = preinit_status->status.state;
  if ((state != EV9_PREINIT_HANDOFF) && (state != EV9_PREINIT_RESTORING) &&
      (state != EV9_PREINIT_ABORTED)) {
    return false;
  }
  return panda->request_ev9_long_preinit_offroad_rearm(
    preinit_status->timing.cycle_started_us);
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
  ps.setCommunicationType(status.communication_type);

  ps.setTimingValid(preinit_status->timing_valid);
  if (preinit_status->timing_valid) {
    const auto &timing = preinit_status->timing;
    ps.setCycleStartedUs(timing.cycle_started_us);
    ps.setLastHostTxUs(timing.last_host_tx_us);
  }
}
