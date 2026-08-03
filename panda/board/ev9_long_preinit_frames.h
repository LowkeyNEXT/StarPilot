#pragma once

#define EV9_PREINIT_REPLAY_COUNT 14U
#define EV9_PREINIT_FRAME_FORCE_NEUTRAL 0x01U
#define EV9_PREINIT_FRAME_SLOW_CLAIM 0x02U
#define EV9_PREINIT_FRAME_CRITICAL 0x04U

typedef struct {
  uint16_t addr;
  uint8_t bus;
  uint8_t len;
  uint8_t flags;
  uint32_t period_us;
  const uint8_t *fallback;
  uint32_t last_rx_us;
  uint32_t last_tx_us;
  uint32_t last_attempt_us;
  uint32_t last_host_tx_us;
  bool captured;
  bool host_claim_reservation_used;
  bool host_claim_reserved;
  bool host_hw_pending;
  CANPacket_t packet;
  CANPacket_t host_hw_packet;
} ev9_preinit_replay_t;

static const uint8_t ev9_preinit_fallback_12a[] =
  "\x00\x00\x00\x02\x40\x00\x08\x00\x00\x00\x00\x00\x00\x64\x00\x00";
static const uint8_t ev9_preinit_fallback_cb[] =
  "\x00\x00\x00\x10\xfb\x3f\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00";
static const uint8_t ev9_preinit_fallback_160[] =
  "\x00\x00\x00\x01\x00\x00\x00\x00\xff\xfc\x01\x00\xa8\x00\x10\x00";
static const uint8_t ev9_preinit_fallback_161[] =
  "\x00\x00\x00\x00\x00\x00\x00\x00\xc0\xff\xf0\xc0\x03\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\x00\x00\x00\x00\x00\x00";
static const uint8_t ev9_preinit_fallback_162[] =
  "\x00\x00\x00\x27\x00\x00\x00\x00\xc0\xff\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00";
static const uint8_t ev9_preinit_fallback_1a0[] =
  "\x00\x00\x00\xfe\xf7\x7f\x64\x00\x00\x00\x00\x00\x00\x08\x00\x00\xff\xf3\x3f\x1e\x0a\x00\x00\x00\xfe\x07\x00\x00\x00\x00\x00\x00";
static const uint8_t ev9_preinit_fallback_1ba[] =
  "\x00\x00\x00\x00\x00\x00\x00\x88\x02\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x0f";
static const uint8_t ev9_preinit_fallback_1da[] =
  "\x00\x00\x00\x22\x00\x11\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00";
static const uint8_t ev9_preinit_fallback_1e0[] =
  "\x00\x00\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00";
static const uint8_t ev9_preinit_fallback_1e5[] =
  "\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x22\x03\x00\x00\x00\x80";
static const uint8_t ev9_preinit_fallback_1ea[] =
  "\x00\x00\x00\x08\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x0f\x0f\x00";
static const uint8_t ev9_preinit_fallback_200[] = "\x00\x00\x00\x14\x80\x1a\x00\x00";
static const uint8_t ev9_preinit_fallback_345[] = "\x00\x00\x00\x15\x00\x56\x00\x00";
static const uint8_t ev9_preinit_fallback_38c[] =
  "\x00\x00\x00\xf7\x1f\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00";

// One source of truth for every frame Panda owns before host handoff.
static ev9_preinit_replay_t ev9_preinit_replay[EV9_PREINIT_REPLAY_COUNT] = {
  {.addr = 0x12AU, .bus = EV9_PREINIT_BUS_ECAN, .len = 16U, .flags = EV9_PREINIT_FRAME_FORCE_NEUTRAL | EV9_PREINIT_FRAME_CRITICAL, .period_us = 10000U, .fallback = ev9_preinit_fallback_12a},
  {.addr = 0xCBU,  .bus = EV9_PREINIT_BUS_ECAN, .len = 24U, .flags = EV9_PREINIT_FRAME_FORCE_NEUTRAL | EV9_PREINIT_FRAME_CRITICAL, .period_us = 10000U, .fallback = ev9_preinit_fallback_cb},
  {.addr = 0x160U, .bus = EV9_PREINIT_BUS_ECAN, .len = 16U, .flags = EV9_PREINIT_FRAME_FORCE_NEUTRAL | EV9_PREINIT_FRAME_CRITICAL, .period_us = 20000U, .fallback = ev9_preinit_fallback_160},
  {.addr = 0x161U, .bus = EV9_PREINIT_BUS_ECAN, .len = 32U, .flags = EV9_PREINIT_FRAME_FORCE_NEUTRAL, .period_us = 50000U, .fallback = ev9_preinit_fallback_161},
  {.addr = 0x162U, .bus = EV9_PREINIT_BUS_ECAN, .len = 32U, .flags = EV9_PREINIT_FRAME_FORCE_NEUTRAL, .period_us = 50000U, .fallback = ev9_preinit_fallback_162},
  {.addr = 0x1A0U, .bus = EV9_PREINIT_BUS_ECAN, .len = 32U, .flags = EV9_PREINIT_FRAME_FORCE_NEUTRAL | EV9_PREINIT_FRAME_CRITICAL, .period_us = 20000U, .fallback = ev9_preinit_fallback_1a0},
  {.addr = 0x1BAU, .bus = EV9_PREINIT_BUS_ECAN, .len = 24U, .flags = EV9_PREINIT_FRAME_FORCE_NEUTRAL, .period_us = 50000U, .fallback = ev9_preinit_fallback_1ba},
  {.addr = 0x1DAU, .bus = EV9_PREINIT_BUS_ECAN, .len = 32U, .period_us = 1000000U, .fallback = ev9_preinit_fallback_1da},
  {.addr = 0x1E0U, .bus = EV9_PREINIT_BUS_ECAN, .len = 16U, .period_us = 50000U, .fallback = ev9_preinit_fallback_1e0},
  {.addr = 0x1E5U, .bus = EV9_PREINIT_BUS_ECAN, .len = 16U, .flags = EV9_PREINIT_FRAME_FORCE_NEUTRAL, .period_us = 50000U, .fallback = ev9_preinit_fallback_1e5},
  {.addr = 0x1EAU, .bus = EV9_PREINIT_BUS_ECAN, .len = 32U, .period_us = 50000U, .fallback = ev9_preinit_fallback_1ea},
  {.addr = 0x200U, .bus = EV9_PREINIT_BUS_ECAN, .len = 8U, .period_us = 50000U, .fallback = ev9_preinit_fallback_200},
  {.addr = 0x345U, .bus = EV9_PREINIT_BUS_ECAN, .len = 8U, .flags = EV9_PREINIT_FRAME_SLOW_CLAIM, .period_us = 200000U, .fallback = ev9_preinit_fallback_345},
  {.addr = 0x38CU, .bus = EV9_PREINIT_BUS_ECAN, .len = 32U, .flags = EV9_PREINIT_FRAME_SLOW_CLAIM, .period_us = 200000U, .fallback = ev9_preinit_fallback_38c},
};
