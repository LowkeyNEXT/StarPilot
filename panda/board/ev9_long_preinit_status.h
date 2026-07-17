#pragma once

#define EV9_LONG_PREINIT_STATUS_VERSION 1U

typedef enum {
  EV9_PREINIT_COLLECTING = 0,
  EV9_PREINIT_WAIT_SESSION,
  EV9_PREINIT_WAIT_COMM_CONTROL,
  EV9_PREINIT_WAIT_SUPPRESSION,
  EV9_PREINIT_ACTIVE,
  EV9_PREINIT_HANDOFF,
  EV9_PREINIT_ABORTED,
} ev9_preinit_state_t;

typedef struct __attribute__((packed)) {
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
} ev9_long_preinit_status_t;
