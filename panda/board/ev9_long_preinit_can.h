#pragma once

// CAN queue integration for the explicitly selected EV9 resident-preinit
// firmware. Keep the generic CAN driver limited to call sites; ownership,
// reservation, and receipt behavior belongs here.

static void ev9_long_preinit_can_queue_cleared(can_ring *q) {
#ifdef PANDA_EV9_LONG_PREINIT
  ENTER_CRITICAL();
  for (uint8_t i = 0U; i < PANDA_CAN_CNT; i++) {
    if (q == can_queues[i]) {
      ev9_long_preinit_tx_queue_cleared(i);
    }
  }
  EXIT_CRITICAL();
#else
  (void)q;
#endif
}

#ifdef PANDA_EV9_LONG_PREINIT
static bool ev9_can_send_with_result(CANPacket_t *to_push, uint8_t bus_number,
                                     bool skip_tx_hook, bool ev9_preinit_internal) {
  bool queued = false;
  ENTER_CRITICAL();
  const bool preinit_tx_allowed = ev9_preinit_internal ?
    ev9_long_preinit_internal_tx_allowed(to_push, bus_number) :
    ev9_long_preinit_external_tx_allowed(to_push, bus_number, skip_tx_hook);
  bool preinit_tx_ready = true;
  if (preinit_tx_allowed && !ev9_preinit_internal) {
    preinit_tx_ready = ev9_long_preinit_prepare_host_tx(to_push, bus_number, skip_tx_hook);
  }

  if (preinit_tx_allowed && preinit_tx_ready && (skip_tx_hook || safety_tx_hook(to_push) != 0)) {
    if (bus_number < PANDA_CAN_CNT) {
      queued = can_push(can_queues[bus_number], to_push);
      tx_buffer_overflow += queued ? 0U : 1U;
      if (queued && !skip_tx_hook) {
        ev9_long_preinit_host_tx_hook(to_push);
      }
      process_can(CAN_NUM_FROM_BUS_NUM(bus_number));
    }
  } else if (!skip_tx_hook && (!preinit_tx_allowed || preinit_tx_ready)) {
    safety_tx_blocked += 1U;
    to_push->returned = 0U;
    to_push->rejected = 1U;
    can_set_checksum(to_push);
    rx_buffer_overflow += can_push(&can_rx_q, to_push) ? 0U : 1U;
  } else {
    // Forwarding is silently quiesced during EV9 restore/rearm. Emitting a
    // rejected receipt for every physical frame would flood the host RX queue.
  }
  EXIT_CRITICAL();
  return queued;
}

bool can_send_with_result(CANPacket_t *to_push, uint8_t bus_number, bool skip_tx_hook) {
  return ev9_can_send_with_result(to_push, bus_number, skip_tx_hook, false);
}

bool can_send_ev9_preinit_with_result(CANPacket_t *to_push, uint8_t bus_number) {
  return ev9_can_send_with_result(to_push, bus_number, true, true);
}
#endif
