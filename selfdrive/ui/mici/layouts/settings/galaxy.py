import hashlib
import json
import math
import secrets
import string
from pathlib import Path

import numpy as np
import pyray as rl
import qrcode

from openpilot.common.swaglog import cloudlog
from openpilot.selfdrive.ui.ui_state import device, ui_state
from openpilot.selfdrive.ui.mici.widgets.button import BigButton
from openpilot.selfdrive.ui.mici.widgets.dialog import BigDialog, BigConfirmationDialog, BigInputDialog, BigMultiOptionDialog
from openpilot.system.hardware import PC
from openpilot.system.hardware.hw import Paths
from openpilot.system.vehicle_telemetry.obd import DEFAULT_OBD_BLE_NAME, normalize_obd_ble_name
from openpilot.system.vehicle_telemetry.obd_bluez import PAIRING_WINDOW_SECONDS
from openpilot.system.vehicle_telemetry.setup import launch_vehicle_telemetry_setup
from openpilot.system.ui.lib.application import FontWeight, gui_app
from openpilot.system.ui.lib.text_measure import measure_text_cached
from openpilot.system.ui.widgets.label import UnifiedLabel
from openpilot.system.ui.widgets.nav_widget import NavWidget


class GalaxyQRDialog(NavWidget):
  def __init__(self, url: str, title: str = "pair with galaxy"):
    super().__init__()
    self._url = url
    self._qr_texture: rl.Texture | None = None
    self._title = UnifiedLabel(title, font_size=48, font_weight=FontWeight.BOLD, line_height=0.8)
    self._generate_qr_code()

  def _generate_qr_code(self) -> None:
    try:
      qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=10, border=0)
      qr.add_data(self._url)
      qr.make(fit=True)

      pil_img = qr.make_image(fill_color="white", back_color="black").convert("RGBA")
      img_array = np.array(pil_img, dtype=np.uint8)

      if self._qr_texture and self._qr_texture.id != 0:
        rl.unload_texture(self._qr_texture)

      rl_image = rl.Image()
      rl_image.data = rl.ffi.cast("void *", img_array.ctypes.data)
      rl_image.width = pil_img.width
      rl_image.height = pil_img.height
      rl_image.mipmaps = 1
      rl_image.format = rl.PixelFormat.PIXELFORMAT_UNCOMPRESSED_R8G8B8A8
      self._qr_texture = rl.load_texture_from_image(rl_image)
    except Exception as e:
      cloudlog.warning(f"Galaxy QR generation failed: {e}")
      self._qr_texture = None

  def _render(self, rect: rl.Rectangle):
    if self._qr_texture is None:
      rl.draw_text_ex(gui_app.font(FontWeight.BOLD), "QR Code Error", rl.Vector2(rect.x + 20, rect.y + rect.height / 2 - 15), 30, 0.0, rl.RED)
      return

    scale = rect.height / self._qr_texture.height
    pos = rl.Vector2(round(rect.x + 8), round(rect.y))
    rl.draw_texture_ex(self._qr_texture, pos, 0.0, scale, rl.WHITE)

    label_x = rect.x + 8 + rect.height + 24
    self._title.set_max_width(int(rect.width - label_x))
    self._title.set_position(label_x, rect.y + 16)
    self._title.render()

  def __del__(self):
    if self._qr_texture and self._qr_texture.id != 0:
      rl.unload_texture(self._qr_texture)


class GalaxyBigButton(BigButton):
  _SLUG_CHARS = string.ascii_letters + string.digits

  def __init__(self):
    super().__init__("galaxy", "", gui_app.texture("icons_mici/settings/galaxy.png", 64, 64))
    self._galaxy_dir = Path(Paths.comma_home()) / "starpilot" / "data" / "galaxy" if PC else Path("/data/galaxy")
    self._auth_path = self._galaxy_dir / "glxyauth"
    self._session_path = self._galaxy_dir / "glxysession"
    self._slug_path = self._galaxy_dir / "glxyslug"

  def _get_label_font_size(self):
    return 64

  def _is_paired(self) -> bool:
    try:
      return len(self._auth_path.read_text(encoding="utf-8").strip()) == 64
    except Exception:
      return False

  def _get_slug(self) -> str:
    try:
      return self._slug_path.read_text(encoding="utf-8").strip()
    except Exception:
      return ""

  def _show_qr(self):
    slug = self._get_slug()
    if not slug:
      gui_app.push_widget(BigDialog("", "Galaxy is not paired yet."))
      return
    gui_app.push_widget(GalaxyQRDialog(f"https://galaxy.firestar.link/{slug}"))

  def _pair_with_password(self, password: str):
    clean_password = str(password or "").strip()
    if len(clean_password) < 6:
      gui_app.push_widget(BigDialog("", "Password must be at least 6 characters."))
      return

    try:
      self._galaxy_dir.mkdir(parents=True, exist_ok=True)
      self._auth_path.write_text(hashlib.sha256(clean_password.encode("utf-8")).hexdigest(), encoding="utf-8")
      self._session_path.write_text(secrets.token_hex(32), encoding="utf-8")
      slug = "".join(secrets.choice(self._SLUG_CHARS) for _ in range(16))
      self._slug_path.write_text(slug, encoding="utf-8")
    except Exception as e:
      cloudlog.warning(f"Galaxy pairing write failed: {e}")
      gui_app.push_widget(BigDialog("", "Failed to pair with Galaxy."))
      return

    self._show_qr()

  def _unpair(self):
    for path in (self._auth_path, self._session_path, self._slug_path):
      try:
        path.unlink(missing_ok=True)
      except TypeError:
        if path.exists():
          path.unlink()
      except Exception as e:
        cloudlog.warning(f"Galaxy unpair cleanup failed for {path}: {e}")

  def _handle_mouse_release(self, mouse_pos):
    super()._handle_mouse_release(mouse_pos)

    if self._is_paired():
      dialog_holder: dict[str, BigMultiOptionDialog] = {}

      def on_confirm():
        selection = dialog_holder["dialog"].get_selected_option()
        if selection == "show qr":
          self._show_qr()
        elif selection == "unpair":
          gui_app.push_widget(
            BigConfirmationDialog(
              "slide to\nunpair galaxy",
              gui_app.texture("icons_mici/settings/device/uninstall.png", 64, 64),
              self._unpair,
              red=True,
            )
          )

      dialog = BigMultiOptionDialog(options=["show qr", "unpair"], default="show qr", right_btn_callback=on_confirm)
      dialog_holder["dialog"] = dialog
      gui_app.push_widget(dialog)
      return

    gui_app.push_widget(BigInputDialog("set galaxy password...", default_text="", minimum_length=6, confirm_callback=self._pair_with_password))

  def _update_state(self):
    self.set_value("paired" if self._is_paired() else "pair")


class TelemetrySetupBigButton(BigButton):
  def __init__(self):
    super().__init__("EV vehicle\ntelemetry", "scan QR", gui_app.texture("icons_mici/settings/galaxy.png", 64, 64))
    self._data_dir = Path(Paths.comma_home()) / "starpilot" / "data" / "galaxy" if PC else Path("/data/galaxy")

  def _handle_mouse_release(self, mouse_pos):
    super()._handle_mouse_release(mouse_pos)
    try:
      session = launch_vehicle_telemetry_setup(self._data_dir)
      gui_app.push_widget(GalaxyQRDialog(session["url"], "set up EV telemetry"))
    except Exception as error:
      cloudlog.warning(f"Vehicle telemetry setup launch failed: {error}")
      gui_app.push_widget(BigDialog("", str(error)))


class ObdBlePairingDialog(NavWidget):
  def __init__(self):
    super().__init__()
    self._params = ui_state.params
    self._remaining = PAIRING_WINDOW_SECONDS
    self.set_back_callback(self._cancel_pairing)

  @staticmethod
  def _text(value):
    if isinstance(value, bytes):
      return value.decode("utf-8", errors="replace")
    return str(value or "")

  def _draw_centered(self, rect, text, y, size, color, weight=FontWeight.NORMAL):
    font = gui_app.font(weight)
    dimensions = measure_text_cached(font, text, size)
    while size > 16 and dimensions.x > rect.width - 32:
      size -= 2
      dimensions = measure_text_cached(font, text, size)
    rl.draw_text_ex(font, text, rl.Vector2(rect.x + (rect.width - dimensions.x) / 2, y), size, 0, color)

  def _cancel_pairing(self):
    self._params.put_bool("ObdBlePairingCancelRequested", True)

  def _retry_pairing(self):
    self._params.put_bool("ObdBlePairingCancelRequested", False)
    self._params.put_bool("ObdBleEnabled", True)
    self._params.put_bool("ObdBlePairingRequested", True)
    self._remaining = PAIRING_WINDOW_SECONDS

  def _handle_mouse_release(self, mouse_pos):
    if self._remaining > 0:
      return
    if self._drag_start_pos is not None and mouse_pos.y - self._drag_start_pos.y > 40:
      return
    self._retry_pairing()

  @staticmethod
  def _draw_progress_border(rect, progress):
    thickness = 8.0
    inset = thickness / 2 + 2
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

  def show_event(self):
    super().show_event()
    device.set_override_interactive_timeout(PAIRING_WINDOW_SECONDS + 30)

  def hide_event(self):
    super().hide_event()
    device.set_override_interactive_timeout(None)

  def _render(self, rect):
    rl.clear_background(rl.BLACK)
    remaining = 0
    status = {}
    try:
      raw_status = self._params.get("ObdBleStatus")
      status = raw_status if isinstance(raw_status, dict) else json.loads(self._text(raw_status) or "{}")
      remaining = int(status.get("pairingRemainingSeconds") or 0)
    except (TypeError, ValueError, json.JSONDecodeError):
      pass
    self._remaining = max(0, remaining)

    local_name = normalize_obd_ble_name(self._params.get("ObdBleName") or DEFAULT_OBD_BLE_NAME)
    passkey = self._text(self._params.get("ObdBlePasskey")).strip()
    self._draw_centered(rect, local_name, rect.y + 22, 30, rl.WHITE, FontWeight.BOLD)
    if status.get("state") == "paired":
      pulse = int(205 + 50 * (0.5 + 0.5 * math.sin(rl.get_time() * 6)))
      self._draw_centered(rect, "BLUETOOTH", rect.y + 55, 19, rl.GRAY, FontWeight.BOLD)
      self._draw_centered(rect, "PAIRED", rect.y + 84, 72, rl.Color(180, 150, pulse, 255), FontWeight.BOLD)
      self._draw_centered(rect, "READY TO USE", rect.y + 177, 24, rl.WHITE, FontWeight.BOLD)
    elif self._remaining == 0:
      self._draw_centered(rect, "PAIRING WINDOW CLOSED", rect.y + 58, 19, rl.GRAY, FontWeight.BOLD)
      self._draw_centered(rect, "TAP TO", rect.y + 84, 50, rl.Color(180, 150, 230, 255), FontWeight.BOLD)
      self._draw_centered(rect, "PAIR AGAIN", rect.y + 137, 50, rl.WHITE, FontWeight.BOLD)
    elif len(passkey) == 6 and passkey.isdigit():
      self._draw_centered(rect, "PAIRING CODE", rect.y + 55, 19, rl.GRAY, FontWeight.BOLD)
      self._draw_centered(rect, f"{passkey[:3]} {passkey[3:]}", rect.y + 80, 82, rl.Color(180, 150, 230, 255), FontWeight.BOLD)
      self._draw_centered(rect, "ENTER THIS CODE ON YOUR IPHONE", rect.y + 177, 24, rl.WHITE, FontWeight.BOLD)
    else:
      self._draw_centered(rect, "PAIRING", rect.y + 55, 19, rl.GRAY, FontWeight.BOLD)
      self._draw_centered(rect, "WAITING", rect.y + 84, 72, rl.Color(180, 150, 230, 255), FontWeight.BOLD)
      self._draw_centered(rect, "SELECT THIS DEVICE IN YOUR OBD APP", rect.y + 177, 23, rl.WHITE, FontWeight.BOLD)

    progress = 1.0 if status.get("state") == "paired" else self._remaining / PAIRING_WINDOW_SECONDS
    self._draw_progress_border(rect, progress)


class ObdBleBigButton(BigButton):
  def __init__(self):
    super().__init__("pairing & devices", "", gui_app.texture("icons_mici/settings/network/bluetooth.png", 64, 64))
    self._params = ui_state.params

  def _start_pairing(self):
    self._params.put_bool("ObdBlePairingCancelRequested", False)
    self._params.put_bool("ObdBleEnabled", True)
    self._params.put_bool("ObdBlePairingRequested", True)
    gui_app.push_widget(ObdBlePairingDialog())

  def _forget(self):
    self._params.put_bool("ObdBleForgetDevicesRequested", True)

  def _rename(self, value):
    self._params.put("ObdBleName", normalize_obd_ble_name(value))

  def _handle_mouse_release(self, mouse_pos):
    super()._handle_mouse_release(mouse_pos)
    if not self._params.get_bool("ObdBleEnabled"):
      self._start_pairing()
      return

    holder = {}

    def on_confirm():
      selection = holder["dialog"].get_selected_option()
      if selection == "pair new phone":
        self._start_pairing()
      elif selection == "rename adapter":
        local_name = normalize_obd_ble_name(self._params.get("ObdBleName") or DEFAULT_OBD_BLE_NAME)
        gui_app.push_widget(BigInputDialog("Bluetooth OBD name...", local_name, minimum_length=1, confirm_callback=self._rename))
      elif selection == "forget phones":
        gui_app.push_widget(BigConfirmationDialog(
          "slide to\nforget phones",
          gui_app.texture("icons_mici/settings/device/uninstall.png", 64, 64),
          self._forget,
          red=True,
        ))
    dialog = BigMultiOptionDialog(
      options=["pair new phone", "rename adapter", "forget phones"],
      default="pair new phone",
      right_btn_callback=on_confirm,
    )
    holder["dialog"] = dialog
    gui_app.push_widget(dialog)

  def _update_state(self):
    self.set_value("manage")
