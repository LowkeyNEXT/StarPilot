#pragma once

typedef enum {
  FIRMWARE_FEATURE_CAN_TX_SEND = 0,
  FIRMWARE_FEATURE_CAN_TX_REJECT,
  FIRMWARE_FEATURE_CAN_TX_QUIESCE,
} firmware_feature_can_tx_action_t;

// cppcheck-suppress misra-c2012-20.1
#include "board/ev9_long_preinit_can.h"
