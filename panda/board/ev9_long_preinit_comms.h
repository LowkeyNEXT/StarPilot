#pragma once

// USB control-transfer adapter for EV9 resident preinit. Return -1 to continue
// through Panda's stock request switch, or a non-negative response length when
// the EV9 layer handled, rejected, or short-circuited the request.

static int ev9_long_preinit_comms_handle(const ControlPacket_t *req, uint8_t *resp) {
#ifdef PANDA_EV9_LONG_PREINIT
  if (!ev9_long_preinit_usb_request_allowed(req->request, req->param1, req->param2)) {
    return 0;
  }

  // Pandad writes its complete configuration on every connection. Avoid
  // resetting live CAN cores when the requested state is already installed.
  switch (req->request) {
    case 0xDCU:
      if ((current_safety_mode == req->param1) && (current_safety_param == (uint16_t)req->param2)) {
        return 0;
      }
      break;
    case 0xDEU:
      if ((req->param1 < PANDA_CAN_CNT) && (bus_config[req->param1].can_speed == req->param2)) {
        return 0;
      }
      break;
    case 0xE5U:
      if (can_loopback == (req->param1 > 0U)) {
        return 0;
      }
      break;
    case 0xF9U:
      if ((req->param1 < PANDA_CAN_CNT) && (bus_config[req->param1].can_data_speed == req->param2)) {
        return 0;
      }
      break;
    case 0xFCU:
      if ((req->param1 < PANDA_CAN_CNT) && (bus_config[req->param1].canfd_non_iso == (req->param2 != 0U))) {
        return 0;
      }
      break;
    default:
      break;
  }

  if (req->request == 0xE9U) {
    COMPILE_TIME_ASSERT(sizeof(ev9_long_preinit_status_t) == USBPACKET_MAX_SIZE);
    COMPILE_TIME_ASSERT(sizeof(ev9_long_preinit_timing_t) == USBPACKET_MAX_SIZE);
    if (req->param1 == EV9_LONG_PREINIT_STATUS_PAGE) {
      const ev9_long_preinit_status_t status = ev9_long_preinit_get_status();
      (void)memcpy(resp, (const uint8_t *)&status, sizeof(status));
      return sizeof(status);
    }
    if (req->param1 == EV9_LONG_PREINIT_TIMING_PAGE) {
      const ev9_long_preinit_timing_t timing = ev9_long_preinit_get_timing();
      (void)memcpy(resp, (const uint8_t *)&timing, sizeof(timing));
      return sizeof(timing);
    }
    return 0;
  }

  if (req->request == EV9_PREINIT_USB_CONTROL_REQUEST) {
    if (req->param1 == EV9_PREINIT_USB_RELEASE) {
      resp[0] = ev9_long_preinit_request_release(req->param2) ? 1U : 0U;
      return 1;
    }
    return 0;
  }
#else
  (void)req;
  (void)resp;
#endif
  return -1;
}
