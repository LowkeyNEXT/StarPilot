#pragma once

#define EV9_LONG_PREINIT_STATUS_VERSION 2U

typedef enum {
  EV9_PREINIT_COLLECTING = 0,
  EV9_PREINIT_WAIT_SESSION,
  EV9_PREINIT_WAIT_COMM_CONTROL,
  EV9_PREINIT_WAIT_SUPPRESSION,
  EV9_PREINIT_ACTIVE,
  EV9_PREINIT_HANDOFF,
  EV9_PREINIT_ABORTED,
} ev9_preinit_state_t;

typedef enum {
  EV9_PREINIT_TRIGGER_NONE = 0,
  EV9_PREINIT_TRIGGER_ADAS_WAKE,
  EV9_PREINIT_TRIGGER_START_INTENT,
} ev9_preinit_trigger_t;

typedef struct __attribute__((packed)) {
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
} ev9_long_preinit_status_t;
