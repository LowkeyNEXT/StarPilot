#pragma once

typedef enum {
  FIRMWARE_FEATURE_CAN_TX_SEND = 0,
  FIRMWARE_FEATURE_CAN_TX_REJECT,
  FIRMWARE_FEATURE_CAN_TX_QUIESCE,
} firmware_feature_can_tx_action_t;

#ifdef PANDA_EV9_LONG_PREINIT
#include "board/ev9_long_preinit_can.h"
#else
static void firmware_feature_can_queue_cleared(can_ring *q) {
  (void)q;
}

static void firmware_feature_can_enter(void) {
}

static void firmware_feature_can_exit(void) {
}

static firmware_feature_can_tx_action_t firmware_feature_can_tx_action(
    CANPacket_t *to_push, uint8_t bus_number, bool skip_tx_hook, bool internal) {
  (void)to_push;
  (void)bus_number;
  (void)skip_tx_hook;
  (void)internal;
  return FIRMWARE_FEATURE_CAN_TX_SEND;
}

static void firmware_feature_can_tx_queued(const CANPacket_t *to_push, bool skip_tx_hook) {
  (void)to_push;
  (void)skip_tx_hook;
}
#endif
