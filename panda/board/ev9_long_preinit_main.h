#pragma once

// Main-loop lifecycle adapter for EV9 resident preinit. Generic Panda startup,
// heartbeat, and LED-loop code call these narrow hooks without owning EV9 state.

static bool ev9_long_preinit_preserve_safety_transition(uint16_t mode, uint16_t param) {
#ifdef PANDA_EV9_LONG_PREINIT
  return ev9_long_preinit_preserve_can_on_safety_transition(mode, param);
#else
  (void)mode;
  (void)param;
  return false;
#endif
}

static bool ev9_long_preinit_handle_heartbeat_loss(bool vehicle_live) {
#ifdef PANDA_EV9_LONG_PREINIT
  if (ev9_long_preinit_must_preserve()) {
    if (current_safety_mode != SAFETY_SILENT) {
      // Controls are already disengaged. Revoke host ownership while retaining
      // Panda's neutral bridge until firmware proves the vehicle is OFF.
      ev9_long_preinit_host_watchdog_lost(microsecond_timer_get(), vehicle_live);
    }
    if (power_save_status != POWER_SAVE_STATUS_DISABLED) {
      set_power_save_state(POWER_SAVE_STATUS_DISABLED);
    }
    return true;
  }
#else
  (void)vehicle_live;
#endif
  return false;
}

#ifdef PANDA_EV9_LONG_PREINIT
static void ev9_long_preinit_harness_initialized(uint8_t *previous_status) {
  // harness_init() performs a synchronous orientation measurement. Apply it
  // before the first CAN initialization and avoid a redundant first tick reset.
  can_set_orientation(harness.status == HARNESS_STATUS_FLIPPED);
  *previous_status = harness.status;
}
#endif

static uint16_t ev9_long_preinit_initial_safety_mode(void) {
#ifdef PANDA_EV9_LONG_PREINIT
  return SAFETY_NOOUTPUT;
#else
  return SAFETY_SILENT;
#endif
}

static void ev9_long_preinit_main_init(void) {
#ifdef PANDA_EV9_LONG_PREINIT
  ev9_long_preinit_init();
#endif
}

static void ev9_long_preinit_main_tick(void) {
#ifdef PANDA_EV9_LONG_PREINIT
  ev9_long_preinit_tick(microsecond_timer_get(), panda_ignition_line());
#endif
}

static void ev9_long_preinit_main_service_tx_cancel(void) {
#ifdef PANDA_EV9_LONG_PREINIT
  ev9_long_preinit_service_tx_cancel(microsecond_timer_get());
#endif
}
