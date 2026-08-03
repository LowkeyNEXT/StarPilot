#pragma once

// STM32H7 FDCAN ownership reset used only by EV9 resident preinit. The generic
// driver calls into this module at RX, TX-load, and TX-drain boundaries. Reset
// configuration reuses llcan_configure_in_init() from Panda's base H7 driver.

bool ev9_preinit_can_tx_idle(uint8_t bus_number) {
  const uint8_t can_number = CAN_NUM_FROM_BUS_NUM(bus_number);
  FDCAN_GlobalTypeDef *FDCANx = CANIF_FROM_CAN_NUM(can_number);
  can_ring *queue = can_queues[bus_number];
  return (can_slots_empty(queue) == (queue->fifo_size - 1U)) && (FDCANx->TXBRP == 0U);
}

typedef enum {
  EV9_FDCAN_RESET_IDLE = 0,
  EV9_FDCAN_RESET_WAIT_CLOCK,
  EV9_FDCAN_RESET_WAIT_INIT,
  EV9_FDCAN_RESET_WAIT_RX_EMPTY,
  EV9_FDCAN_RESET_CONFIG_RADAR,
  EV9_FDCAN_RESET_CONFIG_ECAN,
  EV9_FDCAN_RESET_WAIT_RUNNING,
  EV9_FDCAN_RESET_DONE,
  EV9_FDCAN_RESET_FAULT,
} ev9_fdcan_reset_state_t;

static ev9_fdcan_reset_state_t ev9_fdcan_reset_state = EV9_FDCAN_RESET_IDLE;
static uint32_t ev9_fdcan_reset_state_started_us = 0U;

void ev9_preinit_can_request_tx_reset(uint32_t now_us) {
  if ((ev9_fdcan_reset_state != EV9_FDCAN_RESET_IDLE) &&
      (ev9_fdcan_reset_state != EV9_FDCAN_RESET_DONE)) {
    return;
  }
  FDCAN_GlobalTypeDef *radar = CANIF_FROM_CAN_NUM(CAN_NUM_FROM_BUS_NUM(EV9_PREINIT_BUS_RADAR));
  FDCAN_GlobalTypeDef *ecan = CANIF_FROM_CAN_NUM(CAN_NUM_FROM_BUS_NUM(EV9_PREINIT_BUS_ECAN));
  // Constant-time request path: RX may call this, so never poll or delay here.
  radar->CCCR &= ~FDCAN_CCCR_CSR;
  ecan->CCCR &= ~FDCAN_CCCR_CSR;
  ev9_fdcan_reset_state = EV9_FDCAN_RESET_WAIT_CLOCK;
  ev9_fdcan_reset_state_started_us = now_us;
}

ev9_preinit_can_reset_result_t ev9_preinit_can_service_tx_reset(uint32_t now_us) {
  const uint8_t radar_number = CAN_NUM_FROM_BUS_NUM(EV9_PREINIT_BUS_RADAR);
  const uint8_t ecan_number = CAN_NUM_FROM_BUS_NUM(EV9_PREINIT_BUS_ECAN);
  FDCAN_GlobalTypeDef *radar = CANIF_FROM_CAN_NUM(radar_number);
  FDCAN_GlobalTypeDef *ecan = CANIF_FROM_CAN_NUM(ecan_number);

  if ((ev9_fdcan_reset_state != EV9_FDCAN_RESET_IDLE) &&
      (ev9_fdcan_reset_state != EV9_FDCAN_RESET_DONE) &&
      (ev9_fdcan_reset_state != EV9_FDCAN_RESET_FAULT) &&
      (get_ts_elapsed(now_us, ev9_fdcan_reset_state_started_us) >= EV9_PREINIT_CAN_RESET_TIMEOUT_US)) {
    ev9_fdcan_reset_state = EV9_FDCAN_RESET_FAULT;
  }

  switch (ev9_fdcan_reset_state) {
    case EV9_FDCAN_RESET_IDLE:
      return EV9_PREINIT_CAN_RESET_IDLE;
    case EV9_FDCAN_RESET_WAIT_CLOCK:
      if (((radar->CCCR & FDCAN_CCCR_CSA) == 0U) && ((ecan->CCCR & FDCAN_CCCR_CSA) == 0U)) {
        radar->CCCR |= FDCAN_CCCR_INIT;
        ecan->CCCR |= FDCAN_CCCR_INIT;
        ev9_fdcan_reset_state = EV9_FDCAN_RESET_WAIT_INIT;
        ev9_fdcan_reset_state_started_us = now_us;
      }
      break;
    case EV9_FDCAN_RESET_WAIT_INIT:
      if (((radar->CCCR & FDCAN_CCCR_INIT) != 0U) && ((ecan->CCCR & FDCAN_CCCR_INIT) != 0U)) {
        ev9_fdcan_reset_state = EV9_FDCAN_RESET_WAIT_RX_EMPTY;
        ev9_fdcan_reset_state_started_us = now_us;
      }
      break;
    case EV9_FDCAN_RESET_WAIT_RX_EMPTY:
      if (((radar->RXF0S & FDCAN_RXF0S_F0FL) == 0U) &&
          ((ecan->RXF0S & FDCAN_RXF0S_F0FL) == 0U)) {
        can_health[radar_number].can_core_reset_cnt += 1U;
        can_health[ecan_number].can_core_reset_cnt += 1U;
        can_health[radar_number].total_tx_lost_cnt +=
          FDCAN_TX_FIFO_EL_CNT - (radar->TXFQS & FDCAN_TXFQS_TFFL);
        can_health[ecan_number].total_tx_lost_cnt +=
          FDCAN_TX_FIFO_EL_CNT - (ecan->TXFQS & FDCAN_TXFQS_TFFL);
        can_clear(can_queues[EV9_PREINIT_BUS_RADAR]);
        can_clear(can_queues[EV9_PREINIT_BUS_ECAN]);
        ev9_fdcan_reset_state = EV9_FDCAN_RESET_CONFIG_RADAR;
        ev9_fdcan_reset_state_started_us = now_us;
      }
      break;
    case EV9_FDCAN_RESET_CONFIG_RADAR:
      radar->IR |= 0x3FCFFFFFU;
      llcan_configure_in_init(radar, radar_number);
      ev9_fdcan_reset_state = EV9_FDCAN_RESET_CONFIG_ECAN;
      ev9_fdcan_reset_state_started_us = now_us;
      break;
    case EV9_FDCAN_RESET_CONFIG_ECAN:
      ecan->IR |= 0x3FCFFFFFU;
      llcan_configure_in_init(ecan, ecan_number);
      radar->CCCR &= ~FDCAN_CCCR_INIT;
      ecan->CCCR &= ~FDCAN_CCCR_INIT;
      ev9_fdcan_reset_state = EV9_FDCAN_RESET_WAIT_RUNNING;
      ev9_fdcan_reset_state_started_us = now_us;
      break;
    case EV9_FDCAN_RESET_WAIT_RUNNING:
      if (((radar->CCCR & FDCAN_CCCR_INIT) == 0U) && ((ecan->CCCR & FDCAN_CCCR_INIT) == 0U)) {
        llcan_irq_enable(radar);
        llcan_irq_enable(ecan);
        ev9_fdcan_reset_state = EV9_FDCAN_RESET_DONE;
      }
      break;
    case EV9_FDCAN_RESET_DONE:
      ev9_fdcan_reset_state = EV9_FDCAN_RESET_IDLE;
      return EV9_PREINIT_CAN_RESET_COMPLETE;
    case EV9_FDCAN_RESET_FAULT:
      return EV9_PREINIT_CAN_RESET_FAILED;
    default:
      ev9_fdcan_reset_state = EV9_FDCAN_RESET_FAULT;
      return EV9_PREINIT_CAN_RESET_FAILED;
  }
  return (ev9_fdcan_reset_state == EV9_FDCAN_RESET_FAULT) ?
         EV9_PREINIT_CAN_RESET_FAILED : EV9_PREINIT_CAN_RESET_PENDING;
}
