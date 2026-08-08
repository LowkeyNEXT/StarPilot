import json
import math

import pyray as rl

from openpilot.selfdrive.ui.ui_state import device, ui_state
from openpilot.system.vehicle_telemetry.obd import DEFAULT_OBD_BLE_NAME, MAX_OBD_BLE_NAME_BYTES, normalize_obd_ble_name
from openpilot.system.vehicle_telemetry.obd_bluez import PAIRING_WINDOW_SECONDS
from openpilot.system.ui.lib.application import FontWeight, gui_app
from openpilot.system.ui.lib.multilang import tr
from openpilot.system.ui.lib.text_measure import measure_text_cached
from openpilot.system.ui.widgets import DialogResult, Widget
from openpilot.system.ui.widgets.confirm_dialog import ConfirmDialog
from openpilot.system.ui.widgets.keyboard import Keyboard
from openpilot.system.ui.widgets.list_view import ListItem, ToggleAction, button_item
from openpilot.system.ui.widgets.option_dialog import MultiOptionDialog


class ObdBlePairingDialog(Widget):
  def __init__(self, params):
    super().__init__()
    self._params = params
    self._remaining = PAIRING_WINDOW_SECONDS

  @staticmethod
  def _text(value) -> str:
    if isinstance(value, bytes):
      return value.decode("utf-8", errors="replace")
    return str(value or "")

  def _handle_mouse_release(self, _):
    if self._remaining == 0:
      self._params.put_bool("ObdBlePairingCancelRequested", False)
      self._params.put_bool("ObdBleEnabled", True)
      self._params.put_bool("ObdBlePairingRequested", True)
      self._remaining = PAIRING_WINDOW_SECONDS
    else:
      self._params.put_bool("ObdBlePairingCancelRequested", True)
      gui_app.pop_widget()

  def show_event(self):
    super().show_event()
    device.set_override_interactive_timeout(PAIRING_WINDOW_SECONDS + 30)

  def hide_event(self):
    super().hide_event()
    device.set_override_interactive_timeout(None)

  def _render_centered(self, rect, text, y, size, color, weight=FontWeight.NORMAL):
    font = gui_app.font(weight)
    dimensions = measure_text_cached(font, text, size)
    while size > 24 and dimensions.x > rect.width - 80:
      size -= 2
      dimensions = measure_text_cached(font, text, size)
    rl.draw_text_ex(font, text, rl.Vector2(rect.x + (rect.width - dimensions.x) / 2, y), size, 0, color)

  @staticmethod
  def _draw_progress_border(rect, progress):
    thickness = 18.0
    inset = thickness / 2 + 8
    left = rect.x + inset
    top = rect.y + inset
    right = rect.x + rect.width - inset
    bottom = rect.y + rect.height - inset
    width = right - left
    height = bottom - top
    remaining = max(0.0, min(1.0, progress)) * (2 * width + 2 * height)
    color = rl.Color(180, 150, 230, 255)
    segments = (
      (rl.Vector2(left, top), rl.Vector2(right, top), width),
      (rl.Vector2(right, top), rl.Vector2(right, bottom), height),
      (rl.Vector2(right, bottom), rl.Vector2(left, bottom), width),
      (rl.Vector2(left, bottom), rl.Vector2(left, top), height),
    )
    for start, end, length in segments:
      if remaining <= 0:
        break
      drawn = min(remaining, length)
      ratio = drawn / length
      endpoint = rl.Vector2(start.x + (end.x - start.x) * ratio, start.y + (end.y - start.y) * ratio)
      rl.draw_line_ex(start, endpoint, thickness, color)
      remaining -= drawn

  def _render(self, rect):
    rl.clear_background(rl.Color(26, 26, 48, 255))
    local_name = normalize_obd_ble_name(self._params.get("ObdBleName") or DEFAULT_OBD_BLE_NAME)

    remaining = 0
    status = {}
    try:
      raw_status = self._params.get("ObdBleStatus")
      status = raw_status if isinstance(raw_status, dict) else json.loads(self._text(raw_status) or "{}")
      remaining = int(status.get("pairingRemainingSeconds") or 0)
    except (TypeError, ValueError, json.JSONDecodeError):
      pass
    self._remaining = max(0, remaining)

    passkey = self._text(self._params.get("ObdBlePasskey")).strip()
    self._render_centered(rect, local_name, rect.y + 90, 72, rl.WHITE, FontWeight.BOLD)
    if status.get("state") == "paired":
      pulse = int(205 + 50 * (0.5 + 0.5 * math.sin(rl.get_time() * 6)))
      self._render_centered(rect, tr("BLUETOOTH"), rect.y + 215, 42, rl.Color(160, 160, 190, 255), FontWeight.BOLD)
      self._render_centered(rect, tr("PAIRED"), rect.y + 330, 180, rl.Color(180, 150, pulse, 255), FontWeight.BOLD)
      self._render_centered(rect, tr("READY TO USE"), rect.y + 650, 54, rl.WHITE, FontWeight.BOLD)
    elif self._remaining == 0:
      self._render_centered(rect, tr("PAIRING WINDOW CLOSED"), rect.y + 230, 42, rl.Color(160, 160, 190, 255), FontWeight.BOLD)
      self._render_centered(rect, tr("TAP TO PAIR AGAIN"), rect.y + 350, 150, rl.Color(180, 150, 230, 255), FontWeight.BOLD)
    elif len(passkey) == 6 and passkey.isdigit():
      self._render_centered(rect, tr("PAIRING CODE"), rect.y + 215, 42, rl.Color(160, 160, 190, 255), FontWeight.BOLD)
      self._render_centered(rect, f"{passkey[:3]} {passkey[3:]}", rect.y + 300, 220, rl.Color(180, 150, 230, 255), FontWeight.BOLD)
      self._render_centered(rect, tr("ENTER THIS CODE ON YOUR PHONE"), rect.y + 650, 54, rl.WHITE, FontWeight.BOLD)
    else:
      self._render_centered(rect, tr("PAIRING"), rect.y + 215, 42, rl.Color(160, 160, 190, 255), FontWeight.BOLD)
      self._render_centered(rect, tr("WAITING"), rect.y + 330, 180, rl.Color(180, 150, 230, 255), FontWeight.BOLD)
      self._render_centered(rect, tr("SELECT THIS DEVICE IN YOUR OBD APP"), rect.y + 650, 52, rl.WHITE, FontWeight.BOLD)
    if self._remaining > 0 and status.get("state") != "paired":
      self._render_centered(rect, tr("Tap anywhere to cancel"), rect.y + rect.height - 80, 34, rl.Color(160, 160, 190, 255))

    progress = 1.0 if status.get("state") == "paired" else self._remaining / PAIRING_WINDOW_SECONDS
    self._draw_progress_border(rect, progress)


class BluetoothNetworkSettings:
  def __init__(self):
    self._params = ui_state.ui_params
    self._name_keyboard = Keyboard(max_text_size=MAX_OBD_BLE_NAME_BYTES, min_text_size=1)
    self._manage_dialog: MultiOptionDialog | None = None
    self._toggle_action = ToggleAction(initial_state=self._params.get_bool("ObdBleEnabled"))
    self._toggle = ListItem(
      lambda: tr("Bluetooth"),
      description=lambda: tr("Secure ELM327-compatible BLE telemetry. Pairing requires physical access to the comma."),
      action_item=self._toggle_action,
      callback=self._toggle_bluetooth,
    )
    self._manage = button_item(lambda: tr("Pairing & Devices"), lambda: tr("MANAGE"), callback=self._manage_bluetooth)
    self._manage.set_visible(lambda: self._params.get_bool("ObdBleEnabled"))

  @property
  def items(self):
    return [self._toggle, self._manage]

  def _start_pairing(self):
    self._params.put_bool("ObdBlePairingCancelRequested", False)
    self._params.put_bool("ObdBleEnabled", True)
    self._params.put_bool("ObdBlePairingRequested", True)
    self._toggle_action.set_state(True)
    gui_app.push_widget(ObdBlePairingDialog(self._params))

  def _toggle_bluetooth(self):
    enabled = self._toggle_action.get_state()
    if enabled:
      self._start_pairing()
    else:
      self._params.put_bool("ObdBlePairingCancelRequested", True)
      self._params.put_bool("ObdBleEnabled", False)

  def _rename(self, result):
    if result != DialogResult.CONFIRM:
      return
    self._params.put("ObdBleName", normalize_obd_ble_name(self._name_keyboard.text))
    self._name_keyboard.clear()

  def _forget(self, result):
    if result == DialogResult.CONFIRM:
      self._params.put_bool("ObdBleForgetDevicesRequested", True)

  def _manage_bluetooth(self):
    pair = tr("Pair new device")
    rename = tr("Rename adapter")
    forget = tr("Forget bonded devices")

    def on_select(result):
      if result != DialogResult.CONFIRM or not self._manage_dialog:
        return
      if self._manage_dialog.selection == pair:
        self._start_pairing()
      elif self._manage_dialog.selection == rename:
        self._name_keyboard.set_text(normalize_obd_ble_name(self._params.get("ObdBleName") or DEFAULT_OBD_BLE_NAME))
        self._name_keyboard.set_title(tr("Bluetooth name"), tr("Shown to nearby phones and OBD apps."))
        self._name_keyboard.set_callback(self._rename)
        gui_app.push_widget(self._name_keyboard)
      elif self._manage_dialog.selection == forget:
        local_name = normalize_obd_ble_name(self._params.get("ObdBleName") or DEFAULT_OBD_BLE_NAME)
        gui_app.push_widget(ConfirmDialog(tr("Forget all devices bonded to {}?").format(local_name), tr("Forget"), callback=self._forget))

    self._manage_dialog = MultiOptionDialog(tr("Bluetooth"), [pair, rename, forget], pair, callback=on_select)
    gui_app.push_widget(self._manage_dialog)
