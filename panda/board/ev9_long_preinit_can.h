#pragma once

// EV9 policy for Panda's shared CAN transmit path. Queueing, safety receipts,
// and hardware dispatch remain in the base driver so base updates are inherited.

static void firmware_feature_can_queue_cleared(const can_ring *q) {
  ENTER_CRITICAL();
  for (uint8_t i = 0U; i < PANDA_CAN_CNT; i++) {
    if (q == can_queues[i]) {
      ev9_long_preinit_tx_queue_cleared(i);
    }
  }
  EXIT_CRITICAL();
}

static void firmware_feature_can_enter(void) {
  ENTER_CRITICAL();
}

static void firmware_feature_can_exit(void) {
  EXIT_CRITICAL();
}

static firmware_feature_can_tx_action_t firmware_feature_can_tx_action(
    CANPacket_t *to_push, uint8_t bus_number, bool skip_tx_hook, bool internal) {
  const bool preinit_tx_allowed = internal ?
    ev9_long_preinit_internal_tx_allowed(to_push, bus_number) :
    ev9_long_preinit_external_tx_allowed(to_push, bus_number, skip_tx_hook);
  if (!preinit_tx_allowed) {
    // Forwarding is silently quiesced during EV9 restore/rearm. Emitting a
    // rejected receipt for every physical frame would flood the host RX queue.
    return skip_tx_hook ? FIRMWARE_FEATURE_CAN_TX_QUIESCE : FIRMWARE_FEATURE_CAN_TX_REJECT;
  }

  if (!internal && !ev9_long_preinit_prepare_host_tx(to_push, bus_number, skip_tx_hook)) {
    return FIRMWARE_FEATURE_CAN_TX_QUIESCE;
  }

  return FIRMWARE_FEATURE_CAN_TX_SEND;
}

static void firmware_feature_can_tx_queued(const CANPacket_t *to_push, bool skip_tx_hook) {
  if (!skip_tx_hook) {
    ev9_long_preinit_host_tx_hook(to_push);
  }
}
