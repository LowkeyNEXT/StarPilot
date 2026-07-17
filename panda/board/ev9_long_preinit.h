#pragma once

#include "board/ev9_long_preinit_status.h"

// Early bridge for the EV9 ADAS communication-control startup race. This is
// only compiled into EV9 preinit firmware and never sends active actuation.

#define EV9_PREINIT_BUS_RADAR 0U
#define EV9_PREINIT_BUS_ECAN 1U
#define EV9_PREINIT_DIAG_ADDR 0x730U
#define EV9_PREINIT_DIAG_RESP_ADDR 0x738U
#define EV9_PREINIT_FINGERPRINT_TIMEOUT_US 700000U
#define EV9_PREINIT_RETRY_INTERVAL_US 200000U
#define EV9_PREINIT_TESTER_PRESENT_INTERVAL_US 100000U
#define EV9_PREINIT_HEARTBEAT_INTERVAL_US 10000U
#define EV9_PREINIT_SUPPRESSION_QUIET_US 60000U
#define EV9_PREINIT_SUPPRESSION_TIMEOUT_US 300000U
#define EV9_PREINIT_REARM_HEARTBEAT_TIMEOUT_US 2000000U
#define EV9_PREINIT_COMM_CONTROL_DELAY_US 50000U
#define EV9_PREINIT_MAX_ATTEMPTS 3U
#define EV9_PREINIT_COMMUNICATION_TYPE 0x01U

typedef struct {
  uint16_t addr;
  uint8_t len;
  uint32_t period_us;
  uint32_t last_tx_us;
  bool required;
  bool captured;
  CANPacket_t packet;
} ev9_preinit_replay_t;

static ev9_preinit_replay_t ev9_preinit_replay[] = {
  {.addr = 0x12AU, .len = 16U, .period_us = 10000U, .required = true},
  {.addr = 0xCBU,  .len = 24U, .period_us = 10000U, .required = true},
  {.addr = 0x160U, .len = 16U, .period_us = 20000U, .required = true},
  {.addr = 0x161U, .len = 32U, .period_us = 50000U},
  {.addr = 0x162U, .len = 32U, .period_us = 50000U},
  {.addr = 0x1A0U, .len = 32U, .period_us = 20000U, .required = true},
  {.addr = 0x1BAU, .len = 24U, .period_us = 50000U, .required = true},
  {.addr = 0x1DAU, .len = 32U, .period_us = 1000000U},
  {.addr = 0x1E0U, .len = 16U, .period_us = 50000U},
  {.addr = 0x1E5U, .len = 16U, .period_us = 50000U},
  {.addr = 0x1EAU, .len = 32U, .period_us = 50000U},
  {.addr = 0x200U, .len = 8U,  .period_us = 50000U},
  {.addr = 0x345U, .len = 8U,  .period_us = 200000U},
  {.addr = 0x38CU, .len = 32U, .period_us = 200000U},
};

static const uint8_t ev9_preinit_heartbeat_template[24] = {
  0x00U, 0x00U, 0x00U, 0x00U, 0xFFU, 0x00U, 0x6FU, 0x00U,
  0xE8U, 0x04U, 0x00U, 0x00U, 0x12U, 0x01U, 0x03U, 0x00U,
  0x55U, 0xFFU, 0xFFU, 0x00U, 0x00U, 0x00U, 0x00U, 0x00U,
};

static const uint8_t ev9_preinit_fallback_161[32] =
  "\x00\x00\x00\x00\x00\x00\x00\x00\xc0\xff\xf0\xc0\x03\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\x00\x00\x00\x00\x00\x00";
static const uint8_t ev9_preinit_fallback_162[32] =
  "\x00\x00\x00\x27\x00\x00\x00\x00\xc0\xff\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00";
static const uint8_t ev9_preinit_fallback_160[16] =
  "\x00\x00\x00\x01\x00\x00\x00\x00\xff\xfc\x01\x00\xa8\x00\x10\x00";
static const uint8_t ev9_preinit_fallback_1da[32] =
  "\x00\x00\x00\x22\x00\x11\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00";
static const uint8_t ev9_preinit_fallback_1e0[16] =
  "\x00\x00\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00";
static const uint8_t ev9_preinit_fallback_1e5[16] =
  "\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x22\x03\x00\x00\x00\x80";
static const uint8_t ev9_preinit_fallback_1ea[32] =
  "\x00\x00\x00\x08\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x0f\x0f\x00";
static const uint8_t ev9_preinit_fallback_200[8] = "\x00\x00\x00\x14\x80\x1a\x00\x00";
static const uint8_t ev9_preinit_fallback_345[8] = "\x00\x00\x00\x15\x00\x56\x00\x00";
static const uint8_t ev9_preinit_fallback_38c[32] =
  "\x00\x00\x00\xf7\x1f\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00";
static ev9_preinit_state_t ev9_preinit_state = EV9_PREINIT_COLLECTING;
static uint32_t ev9_preinit_first_can_us = 0U;
static uint32_t ev9_preinit_state_started_us = 0U;
static uint32_t ev9_preinit_last_tester_present_us = 0U;
static uint32_t ev9_preinit_last_heartbeat_tx_us = 0U;
static uint32_t ev9_preinit_last_vehicle_frame_us = 0U;
static uint32_t ev9_preinit_last_adas_frame_us = 0U;
static uint32_t ev9_preinit_last_stock_heartbeat_us = 0U;
static uint8_t ev9_preinit_attempts = 0U;
static uint8_t ev9_preinit_fingerprint = 0U;
static uint8_t ev9_preinit_heartbeat_counter = 0U;
static bool ev9_preinit_host_heartbeat = false;
static bool ev9_preinit_host_scc = false;
static uint8_t ev9_preinit_last_service = 0U;
static uint8_t ev9_preinit_last_response = 0U;
static uint8_t ev9_preinit_last_nrc = 0U;
static CANPacket_t ev9_preinit_heartbeat_packet;

#define EV9_FP_HEARTBEAT 0x01U
#define EV9_FP_POWERTRAIN 0x02U
#define EV9_FP_WHEEL_SPEEDS 0x04U
#define EV9_FP_SCC 0x08U
#define EV9_FP_FCA_STATUS 0x10U
#define EV9_FP_BSM_STATUS 0x20U
#define EV9_FP_REQUIRED 0x3FU

static void ev9_preinit_set_bits(uint8_t *data, uint16_t start, uint8_t size, uint32_t value) {
  for (uint8_t i = 0U; i < size; i++) {
    const uint16_t bit = start + i;
    const uint8_t mask = (uint8_t)(1U << (bit % 8U));
    if ((value & (1UL << i)) != 0U) {
      data[bit / 8U] |= mask;
    } else {
      data[bit / 8U] &= (uint8_t)(~mask);
    }
  }
}

static uint16_t ev9_preinit_crc(const CANPacket_t *packet) {
  const uint8_t len = GET_LEN(packet);
  if ((len == 24U) || (len == 32U)) {
    return (uint16_t)hyundai_common_canfd_compute_checksum(packet);
  }

  uint16_t crc = 0U;
  for (uint8_t i = 2U; i < len; i++) {
    crc = (uint16_t)((crc << 8U) ^ hyundai_canfd_crc_lut[(crc >> 8U) ^ packet->data[i]]);
  }
  crc = (uint16_t)((crc << 8U) ^ hyundai_canfd_crc_lut[(crc >> 8U) ^ (packet->addr & 0xFFU)]);
  crc = (uint16_t)((crc << 8U) ^ hyundai_canfd_crc_lut[(crc >> 8U) ^ ((packet->addr >> 8U) & 0xFFU)]);
  if (len == 8U) {
    crc ^= 0x5F29U;
  } else {
    crc ^= 0x041DU;
  }
  return crc;
}

static void ev9_preinit_update_crc(CANPacket_t *packet) {
  const uint16_t crc = ev9_preinit_crc(packet);
  packet->data[0] = (uint8_t)(crc & 0xFFU);
  packet->data[1] = (uint8_t)(crc >> 8U);
  can_set_checksum(packet);
}

static CANPacket_t ev9_preinit_make_packet(uint16_t addr, uint8_t bus, uint8_t len) {
  CANPacket_t packet = {0};
  packet.fd = addr != EV9_PREINIT_DIAG_ADDR;
  packet.bus = bus;
  packet.addr = addr;
  for (uint8_t dlc = 0U; dlc < 16U; dlc++) {
    if (dlc_to_len[dlc] == len) {
      packet.data_len_code = dlc;
      break;
    }
  }
  return packet;
}

static void ev9_preinit_send_diag(uint8_t service, uint8_t subfunction, uint8_t control_type) {
  ev9_preinit_last_service = service;
  ev9_preinit_last_response = 0U;
  ev9_preinit_last_nrc = 0U;
  CANPacket_t packet = ev9_preinit_make_packet(EV9_PREINIT_DIAG_ADDR, EV9_PREINIT_BUS_ECAN, 8U);
  if (service == 0x28U) {
    packet.data[0] = 3U;
    packet.data[1] = service;
    packet.data[2] = subfunction;
    packet.data[3] = control_type;
  } else {
    packet.data[0] = 2U;
    packet.data[1] = service;
    packet.data[2] = subfunction;
  }
  can_set_checksum(&packet);
  can_send(&packet, EV9_PREINIT_BUS_ECAN, true);
}

static void ev9_preinit_restore(void) {
  ev9_preinit_send_diag(0x28U, 0x00U, EV9_PREINIT_COMMUNICATION_TYPE);
}

static void ev9_preinit_abort(uint32_t now_us) {
  ev9_preinit_state = EV9_PREINIT_ABORTED;
  ev9_preinit_state_started_us = now_us;
}

static void ev9_long_preinit_init(void) {
  ev9_preinit_state = EV9_PREINIT_COLLECTING;
  ev9_preinit_first_can_us = 0U;
  ev9_preinit_state_started_us = 0U;
  ev9_preinit_last_tester_present_us = 0U;
  ev9_preinit_last_heartbeat_tx_us = 0U;
  ev9_preinit_last_vehicle_frame_us = 0U;
  ev9_preinit_last_adas_frame_us = 0U;
  ev9_preinit_last_stock_heartbeat_us = 0U;
  ev9_preinit_attempts = 0U;
  ev9_preinit_fingerprint = 0U;
  ev9_preinit_heartbeat_counter = 0U;
  ev9_preinit_host_heartbeat = false;
  ev9_preinit_host_scc = false;
  ev9_preinit_last_service = 0U;
  ev9_preinit_last_response = 0U;
  ev9_preinit_last_nrc = 0U;
  ev9_preinit_heartbeat_packet = ev9_preinit_make_packet(0x100U, EV9_PREINIT_BUS_RADAR, 24U);
  (void)memcpy(ev9_preinit_heartbeat_packet.data, ev9_preinit_heartbeat_template, sizeof(ev9_preinit_heartbeat_template));
  for (uint8_t i = 0U; i < (sizeof(ev9_preinit_replay) / sizeof(ev9_preinit_replay[0])); i++) {
    ev9_preinit_replay[i].captured = false;
    ev9_preinit_replay[i].last_tx_us = 0U;
    const uint8_t *fallback = NULL;
    switch (ev9_preinit_replay[i].addr) {
      case 0x161U: fallback = ev9_preinit_fallback_161; break;
      case 0x162U: fallback = ev9_preinit_fallback_162; break;
      case 0x1DAU: fallback = ev9_preinit_fallback_1da; break;
      case 0x1E0U: fallback = ev9_preinit_fallback_1e0; break;
      case 0x1E5U: fallback = ev9_preinit_fallback_1e5; break;
      case 0x1EAU: fallback = ev9_preinit_fallback_1ea; break;
      case 0x200U: fallback = ev9_preinit_fallback_200; break;
      case 0x345U: fallback = ev9_preinit_fallback_345; break;
      case 0x38CU: fallback = ev9_preinit_fallback_38c; break;
      default: break;
    }
    if (fallback != NULL) {
      ev9_preinit_replay[i].packet = ev9_preinit_make_packet(ev9_preinit_replay[i].addr, EV9_PREINIT_BUS_ECAN,
                                                             ev9_preinit_replay[i].len);
      (void)memcpy(ev9_preinit_replay[i].packet.data, fallback, ev9_preinit_replay[i].len);
      ev9_preinit_replay[i].captured = true;
    }
  }
}

static bool ev9_preinit_is_stock_heartbeat(const CANPacket_t *packet) {
  const uint16_t checksum = (uint16_t)packet->data[0] | ((uint16_t)packet->data[1] << 8U);
  if (!packet->fd || (packet->bus != EV9_PREINIT_BUS_RADAR) || (packet->addr != 0x100U) ||
      (GET_LEN(packet) != 24U) || (ev9_preinit_crc(packet) != checksum)) {
    return false;
  }
  return (packet->data[3] == 0x00U) && (packet->data[7] == 0xFFU) &&
         (packet->data[8] == 0x00U) && (packet->data[9] == 0x00U) &&
         (packet->data[10] == 0x00U) && (packet->data[11] == 0x00U) &&
         (packet->data[14] == 0x00U) && (packet->data[17] == 0xFFU) &&
         (packet->data[18] == 0xFFU) && (packet->data[19] == 0x00U) &&
         (packet->data[21] == 0x00U) && (packet->data[22] == 0x00U) &&
         (packet->data[23] == 0x00U);
}

static bool ev9_preinit_required_baselines_ready(void) {
  bool ready = true;
  for (uint8_t i = 0U; i < (sizeof(ev9_preinit_replay) / sizeof(ev9_preinit_replay[0])); i++) {
    ready = ready && (!ev9_preinit_replay[i].required || ev9_preinit_replay[i].captured);
  }
  return ready;
}

static bool ev9_preinit_is_adas_frame(const CANPacket_t *packet) {
  bool adas_frame = (packet->bus == EV9_PREINIT_BUS_RADAR) && (packet->addr == 0x100U) && (GET_LEN(packet) == 24U);
  if (packet->bus == EV9_PREINIT_BUS_ECAN) {
    for (uint8_t i = 0U; i < (sizeof(ev9_preinit_replay) / sizeof(ev9_preinit_replay[0])); i++) {
      adas_frame = adas_frame || ((packet->addr == ev9_preinit_replay[i].addr) &&
                                  (GET_LEN(packet) == ev9_preinit_replay[i].len));
    }
  }
  return adas_frame;
}

static void ev9_preinit_neutralize(CANPacket_t *packet) {
  if (packet->addr == 0x1A0U) {
    ev9_preinit_set_bits(packet->data, 68U, 3U, 0U);   // ACCMode
    ev9_preinit_set_bits(packet->data, 128U, 11U, 1023U); // aReqValue = 0
    ev9_preinit_set_bits(packet->data, 140U, 11U, 1023U); // aReqRaw = 0
    ev9_preinit_set_bits(packet->data, 184U, 1U, 0U);  // StopReq
  } else if (packet->addr == 0x12AU) {
    ev9_preinit_set_bits(packet->data, 41U, 11U, 1024U); // zero torque
    ev9_preinit_set_bits(packet->data, 52U, 1U, 0U);  // STEER_REQ
    ev9_preinit_set_bits(packet->data, 62U, 1U, 0U);  // LKA_ASSIST
    ev9_preinit_set_bits(packet->data, 65U, 3U, 0U);  // STEER_MODE
  } else if (packet->addr == 0xCBU) {
    ev9_preinit_set_bits(packet->data, 24U, 4U, 0U);  // ADAS_ActvACISta
    ev9_preinit_set_bits(packet->data, 28U, 4U, 1U);  // inactive angle status
    ev9_preinit_set_bits(packet->data, 48U, 8U, 0U);  // torque reduction gain
    ev9_preinit_set_bits(packet->data, 56U, 2U, 0U);  // FCA torque request
    ev9_preinit_set_bits(packet->data, 64U, 8U, 0U);  // FCA torque gain
  } else if (packet->addr == 0x161U) {
    ev9_preinit_set_bits(packet->data, 24U, 3U, 1U);  // expected FCA unavailable icon
    ev9_preinit_set_bits(packet->data, 27U, 3U, 0U);
    ev9_preinit_set_bits(packet->data, 42U, 3U, 0U);
  }
}

static void ev9_long_preinit_rx_hook(const CANPacket_t *packet, uint32_t now_us) {
  const bool stock_heartbeat = ev9_preinit_is_stock_heartbeat(packet);
  if (stock_heartbeat) {
    ev9_preinit_last_stock_heartbeat_us = now_us;
  }
  if (((packet->bus == EV9_PREINIT_BUS_ECAN) && (packet->addr == 0x35U) && (GET_LEN(packet) == 32U)) ||
      ((packet->bus == EV9_PREINIT_BUS_ECAN) && (packet->addr == 0xA0U) && (GET_LEN(packet) == 24U))) {
    ev9_preinit_last_vehicle_frame_us = now_us;
  }
  if ((ev9_preinit_state == EV9_PREINIT_ABORTED) || (ev9_preinit_state == EV9_PREINIT_HANDOFF)) {
    return;
  }

  if ((ev9_preinit_state == EV9_PREINIT_WAIT_SUPPRESSION) && ev9_preinit_is_adas_frame(packet)) {
    ev9_preinit_last_adas_frame_us = now_us;
  }

  if (ev9_preinit_state == EV9_PREINIT_COLLECTING) {
    const uint16_t checksum = (uint16_t)packet->data[0] | ((uint16_t)packet->data[1] << 8U);
    const bool valid_canfd_crc = packet->fd && (ev9_preinit_crc(packet) == checksum);
    if (stock_heartbeat) {
      ev9_preinit_fingerprint |= EV9_FP_HEARTBEAT;
      if (ev9_preinit_first_can_us == 0U) {
        ev9_preinit_first_can_us = now_us;
      }
    } else if (valid_canfd_crc && (packet->bus == EV9_PREINIT_BUS_ECAN) && (packet->addr == 0x35U) && (GET_LEN(packet) == 32U)) {
      ev9_preinit_fingerprint |= EV9_FP_POWERTRAIN;
    } else if (valid_canfd_crc && (packet->bus == EV9_PREINIT_BUS_ECAN) && (packet->addr == 0xA0U) && (GET_LEN(packet) == 24U)) {
      ev9_preinit_fingerprint |= EV9_FP_WHEEL_SPEEDS;
    } else if (valid_canfd_crc && (packet->bus == EV9_PREINIT_BUS_ECAN) && (packet->addr == 0x1A0U) && (GET_LEN(packet) == 32U)) {
      ev9_preinit_fingerprint |= EV9_FP_SCC;
    } else if (valid_canfd_crc && (packet->bus == EV9_PREINIT_BUS_ECAN) && (packet->addr == 0x160U) && (GET_LEN(packet) == 16U)) {
      ev9_preinit_fingerprint |= EV9_FP_FCA_STATUS;
    } else if (valid_canfd_crc && (packet->bus == EV9_PREINIT_BUS_ECAN) && (packet->addr == 0x1BAU) && (GET_LEN(packet) == 24U)) {
      ev9_preinit_fingerprint |= EV9_FP_BSM_STATUS;
    } else {
    }

    if (packet->bus == EV9_PREINIT_BUS_ECAN) {
      for (uint8_t i = 0U; i < (sizeof(ev9_preinit_replay) / sizeof(ev9_preinit_replay[0])); i++) {
        if ((packet->addr == ev9_preinit_replay[i].addr) && (GET_LEN(packet) == ev9_preinit_replay[i].len) && valid_canfd_crc) {
          if (packet->addr == 0x160U) {
            ev9_preinit_replay[i].packet = ev9_preinit_make_packet(0x160U, EV9_PREINIT_BUS_ECAN, 16U);
            (void)memcpy(ev9_preinit_replay[i].packet.data, ev9_preinit_fallback_160, sizeof(ev9_preinit_fallback_160));
            ev9_preinit_replay[i].packet.data[2] = packet->data[2];
          } else {
            ev9_preinit_replay[i].packet = *packet;
          }
          ev9_preinit_replay[i].captured = true;
        }
      }
    }

    if ((ev9_preinit_fingerprint == EV9_FP_REQUIRED) && ev9_preinit_required_baselines_ready()) {
      ev9_preinit_attempts = 1U;
      ev9_preinit_state = EV9_PREINIT_WAIT_SESSION;
      ev9_preinit_state_started_us = now_us;
      ev9_preinit_send_diag(0x10U, 0x03U, 0U);
    }
  } else {
  }

  // The EV9 can return eight-byte UDS responses as either classic CAN or CAN FD.
  const bool diag_response = (packet->bus == EV9_PREINIT_BUS_ECAN) && (packet->addr == EV9_PREINIT_DIAG_RESP_ADDR) &&
    (GET_LEN(packet) == 8U) && (packet->data[0] >= 3U) && (packet->data[0] <= 7U);
  if (diag_response) {
    ev9_preinit_last_response = packet->data[1];
    ev9_preinit_last_nrc = packet->data[1] == 0x7FU ? packet->data[3] : 0U;
    if ((packet->data[1] == 0x7FU) && ((packet->data[2] == 0x10U) || (packet->data[2] == 0x28U))) {
      const bool retryable = (packet->data[2] == ev9_preinit_last_service) && (packet->data[3] == 0x22U) &&
                             (ev9_preinit_attempts < EV9_PREINIT_MAX_ATTEMPTS);
      if (retryable) {
        ev9_preinit_state_started_us = now_us;
      } else {
        ev9_preinit_abort(now_us);
      }
    } else if ((ev9_preinit_state == EV9_PREINIT_WAIT_SESSION) && (packet->data[1] == 0x50U) && (packet->data[2] == 0x03U)) {
      ev9_preinit_state = EV9_PREINIT_WAIT_COMM_CONTROL;
      ev9_preinit_state_started_us = now_us;
      ev9_preinit_attempts = 0U;
    } else if ((ev9_preinit_state == EV9_PREINIT_WAIT_COMM_CONTROL) && (packet->data[1] == 0x68U) && (packet->data[2] == 0x01U)) {
      // A positive response is not enough: the EV9 also acknowledges 28 01 01
      // without silencing every required stream. Wait for the ADAS frames to stop.
      ev9_preinit_state = EV9_PREINIT_WAIT_SUPPRESSION;
      ev9_preinit_state_started_us = now_us;
      ev9_preinit_last_adas_frame_us = now_us;
    } else {
    }
  }
}

static ev9_long_preinit_status_t ev9_long_preinit_get_status(void) {
  return (ev9_long_preinit_status_t) {
    .version = EV9_LONG_PREINIT_STATUS_VERSION,
    .state = (uint8_t)ev9_preinit_state,
    .fingerprint = ev9_preinit_fingerprint,
    .attempts = ev9_preinit_attempts,
    .last_service = ev9_preinit_last_service,
    .last_response = ev9_preinit_last_response,
    .last_nrc = ev9_preinit_last_nrc,
    .communication_type = EV9_PREINIT_COMMUNICATION_TYPE,
    .first_can_us = ev9_preinit_first_can_us,
    .state_started_us = ev9_preinit_state_started_us,
  };
}

static void ev9_preinit_start_bridge(uint32_t now_us) {
  ev9_preinit_state = EV9_PREINIT_ACTIVE;
  ev9_preinit_last_tester_present_us = now_us - EV9_PREINIT_TESTER_PRESENT_INTERVAL_US;
  ev9_preinit_last_heartbeat_tx_us = now_us - EV9_PREINIT_HEARTBEAT_INTERVAL_US;
  for (uint8_t i = 0U; i < (sizeof(ev9_preinit_replay) / sizeof(ev9_preinit_replay[0])); i++) {
    ev9_preinit_replay[i].last_tx_us = now_us - ev9_preinit_replay[i].period_us;
  }
}

static void ev9_long_preinit_host_tx_hook(const CANPacket_t *packet) {
  if (ev9_preinit_state != EV9_PREINIT_ACTIVE) {
    return;
  }
  ev9_preinit_host_heartbeat = ev9_preinit_host_heartbeat ||
    ((packet->bus == EV9_PREINIT_BUS_RADAR) && (packet->addr == 0x100U) && (GET_LEN(packet) == 24U));
  ev9_preinit_host_scc = ev9_preinit_host_scc ||
    ((packet->bus == EV9_PREINIT_BUS_ECAN) && (packet->addr == 0x1A0U) && (GET_LEN(packet) == 32U));
  if (ev9_preinit_host_heartbeat && ev9_preinit_host_scc) {
    ev9_preinit_state = EV9_PREINIT_HANDOFF;
  }
}

static void ev9_long_preinit_tick(uint32_t now_us) {
  if ((ev9_preinit_state == EV9_PREINIT_ABORTED) || (ev9_preinit_state == EV9_PREINIT_HANDOFF)) {
    const bool aborted_heartbeat_stopped = (ev9_preinit_state == EV9_PREINIT_ABORTED) &&
      (ev9_preinit_last_stock_heartbeat_us != 0U) &&
      (get_ts_elapsed(now_us, ev9_preinit_last_stock_heartbeat_us) > EV9_PREINIT_REARM_HEARTBEAT_TIMEOUT_US);
    const bool vehicle_bus_stopped = (ev9_preinit_last_vehicle_frame_us != 0U) &&
      (get_ts_elapsed(now_us, ev9_preinit_last_vehicle_frame_us) > 5000000U);
    if (aborted_heartbeat_stopped || vehicle_bus_stopped) {
      ev9_long_preinit_init();
    }
    return;
  }
  if (ev9_preinit_state == EV9_PREINIT_COLLECTING) {
    if ((ev9_preinit_first_can_us != 0U) && (get_ts_elapsed(now_us, ev9_preinit_first_can_us) > EV9_PREINIT_FINGERPRINT_TIMEOUT_US)) {
      ev9_preinit_abort(now_us);
    }
    return;
  }
  if ((ev9_preinit_state == EV9_PREINIT_WAIT_SESSION) || (ev9_preinit_state == EV9_PREINIT_WAIT_COMM_CONTROL)) {
    if ((ev9_preinit_state == EV9_PREINIT_WAIT_COMM_CONTROL) && (ev9_preinit_attempts == 0U)) {
      if (get_ts_elapsed(now_us, ev9_preinit_state_started_us) >= EV9_PREINIT_COMM_CONTROL_DELAY_US) {
        ev9_preinit_attempts = 1U;
        ev9_preinit_state_started_us = now_us;
        ev9_preinit_send_diag(0x28U, 0x01U, EV9_PREINIT_COMMUNICATION_TYPE);
      }
      return;
    }
    // Match the host's stock 100 ms response window plus 100 ms retry delay.
    if (get_ts_elapsed(now_us, ev9_preinit_state_started_us) > EV9_PREINIT_RETRY_INTERVAL_US) {
      if (ev9_preinit_attempts < EV9_PREINIT_MAX_ATTEMPTS) {
        ev9_preinit_attempts += 1U;
        ev9_preinit_state_started_us = now_us;
        if (ev9_preinit_state == EV9_PREINIT_WAIT_SESSION) {
          ev9_preinit_send_diag(0x10U, 0x03U, 0U);
        } else {
          ev9_preinit_send_diag(0x28U, 0x01U, EV9_PREINIT_COMMUNICATION_TYPE);
        }
      } else {
        ev9_preinit_abort(now_us);
      }
    }
    return;
  }
  if (ev9_preinit_state == EV9_PREINIT_WAIT_SUPPRESSION) {
    if (get_ts_elapsed(now_us, ev9_preinit_last_adas_frame_us) >= EV9_PREINIT_SUPPRESSION_QUIET_US) {
      ev9_preinit_start_bridge(now_us);
    } else if (get_ts_elapsed(now_us, ev9_preinit_state_started_us) >= EV9_PREINIT_SUPPRESSION_TIMEOUT_US) {
      ev9_preinit_restore();
      ev9_preinit_abort(now_us);
    }
    return;
  }
  if (ev9_preinit_state != EV9_PREINIT_ACTIVE) {
    return;
  }
  // Once the ECU is suppressed, bridge continuously until the host produces
  // both replacement streams. Bus sleep is the only non-host cleanup path.
  if ((ev9_preinit_last_vehicle_frame_us != 0U) &&
      (get_ts_elapsed(now_us, ev9_preinit_last_vehicle_frame_us) > 5000000U)) {
    ev9_preinit_restore();
    ev9_long_preinit_init();
    return;
  }
  // Until host handoff, the returned tester-present frame also advertises that
  // Panda owns the successful knockout. The host resumes the normal 1 Hz rate.
  if (get_ts_elapsed(now_us, ev9_preinit_last_tester_present_us) >= EV9_PREINIT_TESTER_PRESENT_INTERVAL_US) {
    ev9_preinit_send_diag(0x3EU, 0x80U, 0U);
    ev9_preinit_last_tester_present_us = now_us;
  }

  CANPacket_t heartbeat = ev9_preinit_heartbeat_packet;
  if (get_ts_elapsed(now_us, ev9_preinit_last_heartbeat_tx_us) >= EV9_PREINIT_HEARTBEAT_INTERVAL_US) {
    heartbeat.data[2] = ev9_preinit_heartbeat_counter++;
    ev9_preinit_update_crc(&heartbeat);
    can_send(&heartbeat, EV9_PREINIT_BUS_RADAR, true);
    ev9_preinit_heartbeat_packet = heartbeat;
    ev9_preinit_last_heartbeat_tx_us = now_us;
  }

  for (uint8_t i = 0U; i < (sizeof(ev9_preinit_replay) / sizeof(ev9_preinit_replay[0])); i++) {
    ev9_preinit_replay_t *replay = &ev9_preinit_replay[i];
    if (replay->captured && (get_ts_elapsed(now_us, replay->last_tx_us) >= replay->period_us)) {
      CANPacket_t packet = replay->packet;
      packet.data[2] += 1U;
      ev9_preinit_neutralize(&packet);
      ev9_preinit_update_crc(&packet);
      can_send(&packet, EV9_PREINIT_BUS_ECAN, true);
      replay->packet = packet;
      replay->last_tx_us = now_us;
    }
  }
}
