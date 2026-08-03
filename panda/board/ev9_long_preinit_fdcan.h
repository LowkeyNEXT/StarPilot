#pragma once

// STM32H7 FDCAN ownership reset used only by EV9 resident preinit. The generic
// driver calls into this module at RX, TX-load, and TX-drain boundaries.

static void ev9_fdcan_configure_in_init(FDCAN_GlobalTypeDef *FDCANx) {
  uint32_t can_number = CAN_NUM_FROM_CANIF(FDCANx);
  // Caller proves INIT before this constant-work register/RAM configuration.
  FDCANx->CCCR |= FDCAN_CCCR_CCE;
  FDCANx->CCCR &= ~(FDCAN_CCCR_DAR);
  FDCANx->CCCR |= FDCAN_CCCR_TXP;
  FDCANx->CCCR |= FDCAN_CCCR_PXHD;
  FDCANx->CCCR |= (FDCAN_CCCR_FDOE | FDCAN_CCCR_BRSE);
  FDCANx->TXBC &= ~(FDCAN_TXBC_TFQM);
  FDCANx->TXESC |= 0x7U << FDCAN_TXESC_TBDS_Pos;
  FDCANx->RXESC |= 0x7U << FDCAN_RXESC_F0DS_Pos;
  FDCANx->XIDFC &= ~(FDCAN_XIDFC_LSE);
  FDCANx->SIDFC &= ~(FDCAN_SIDFC_LSS);
  FDCANx->GFC &= ~(FDCAN_GFC_RRFE);
  FDCANx->GFC &= ~(FDCAN_GFC_RRFS);
  FDCANx->GFC &= ~(FDCAN_GFC_ANFE);
  FDCANx->GFC &= ~(FDCAN_GFC_ANFS);

  uint32_t RxFIFO0SA = FDCAN_START_ADDRESS + (can_number * FDCAN_OFFSET);
  uint32_t TxFIFOSA = RxFIFO0SA + (FDCAN_RX_FIFO_0_EL_CNT * FDCAN_RX_FIFO_0_EL_SIZE);
  FDCANx->RXF0C |= (FDCAN_RX_FIFO_0_OFFSET + (can_number * FDCAN_OFFSET_W)) << FDCAN_RXF0C_F0SA_Pos;
  FDCANx->RXF0C |= FDCAN_RX_FIFO_0_EL_CNT << FDCAN_RXF0C_F0S_Pos;
  FDCANx->RXF0C |= FDCAN_RXF0C_F0OM;
  FDCANx->TXBC |= (FDCAN_TX_FIFO_OFFSET + (can_number * FDCAN_OFFSET_W)) << FDCAN_TXBC_TBSA_Pos;
  FDCANx->TXBC |= FDCAN_TX_FIFO_EL_CNT << FDCAN_TXBC_TFQS_Pos;

  uint32_t EndAddress = TxFIFOSA + (FDCAN_TX_FIFO_EL_CNT * FDCAN_TX_FIFO_EL_SIZE);
  for (uint32_t RAMcounter = RxFIFO0SA; RAMcounter < EndAddress; RAMcounter += 4U) {
    *(uint32_t *)(RAMcounter) = 0x00000000;
  }

  FDCANx->ILE = (FDCAN_ILE_EINT0 | FDCAN_ILE_EINT1);
  FDCANx->IE = 0U;
  FDCANx->IE |= FDCAN_IE_RF0NE;
  FDCANx->IE |= FDCAN_IE_PEDE | FDCAN_IE_PEAE | FDCAN_IE_BOE | FDCAN_IE_EPE | FDCAN_IE_RF0LE;
  FDCANx->ILS |= FDCAN_ILS_TFEL;
  FDCANx->IE |= FDCAN_IE_TFEE;
}

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
      ev9_fdcan_configure_in_init(radar);
      ev9_fdcan_reset_state = EV9_FDCAN_RESET_CONFIG_ECAN;
      ev9_fdcan_reset_state_started_us = now_us;
      break;
    case EV9_FDCAN_RESET_CONFIG_ECAN:
      ecan->IR |= 0x3FCFFFFFU;
      ev9_fdcan_configure_in_init(ecan);
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
