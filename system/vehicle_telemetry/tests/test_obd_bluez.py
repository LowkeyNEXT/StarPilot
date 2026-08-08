import asyncio

from types import SimpleNamespace

import pytest

from dbus_next import DBusError, Variant
from dbus_next.constants import MessageType

from openpilot.system.vehicle_telemetry.obd import (
  DEFAULT_OBD_BLE_NAME,
  ELM_UART_SERVICE_UUID,
  ObdSessionManager,
  VirtualObdVehicle,
  normalize_obd_ble_name,
)
from openpilot.system.vehicle_telemetry.obd_bluez import (
  ELM_CHARACTERISTIC_PATH,
  ELM_SERVICE_PATH,
  NOTIFY_PATH,
  OBD_UART_SERVICE_UUID,
  SERVICE_PATH,
  WRITE_PATH,
  BluezObdPeripheral,
  ObdNotifyCharacteristic,
  ObdPairingAgent,
  ObdWriteCharacteristic,
  PairingWindow,
)


def test_pairing_window_is_bounded_and_expires():
  now = [100.0]
  window = PairingWindow(clock=lambda: now[0])
  window.open(999)
  assert window.active
  assert window.remaining_seconds == 120
  now[0] = 219.5
  assert window.remaining_seconds == 1
  now[0] = 220.0
  assert not window.active


def test_obd_name_defaults_migrates_and_is_bounded_for_advertising():
  assert normalize_obd_ble_name(None) == DEFAULT_OBD_BLE_NAME
  assert normalize_obd_ble_name("StarPilot OBD") == DEFAULT_OBD_BLE_NAME
  assert normalize_obd_ble_name("  My   Comma  ") == "My Comma"
  assert len(normalize_obd_ble_name("é" * 20).encode("utf-8")) <= 24

  peripheral = BluezObdPeripheral(ObdSessionManager(VirtualObdVehicle(lambda: None)))
  assert peripheral.local_name == DEFAULT_OBD_BLE_NAME
  assert peripheral.advertisement.LocalName == DEFAULT_OBD_BLE_NAME


def test_pairing_agent_requires_window_and_never_accepts_numeric_comparison():
  now = [100.0]
  passkeys = []
  window = PairingWindow(clock=lambda: now[0])
  agent = ObdPairingAgent(window, passkey_callback=passkeys.append)

  with pytest.raises(DBusError):
    agent.DisplayPasskey("/phone", 1234, 0)

  window.open()
  agent.DisplayPasskey("/phone", 1234, 0)
  assert passkeys == ["001234"]
  with pytest.raises(DBusError):
    agent.RequestConfirmation("/phone", 1234)
  with pytest.raises(DBusError):
    agent.AuthorizeService("/phone", "0000180f-0000-1000-8000-00805f9b34fb")
  agent.AuthorizeService("/phone", OBD_UART_SERVICE_UUID)
  agent.AuthorizeService("/phone", ELM_UART_SERVICE_UUID)


def test_gatt_contract_requires_authenticated_encryption():
  peripheral = BluezObdPeripheral(ObdSessionManager(VirtualObdVehicle(lambda: None)))
  objects = peripheral.managed_objects()
  assert set(objects) == {SERVICE_PATH, NOTIFY_PATH, WRITE_PATH, ELM_SERVICE_PATH, ELM_CHARACTERISTIC_PATH}
  assert peripheral.advertisement.ServiceUUIDs == [OBD_UART_SERVICE_UUID, ELM_UART_SERVICE_UUID]

  notify_flags = objects[NOTIFY_PATH]["org.bluez.GattCharacteristic1"]["Flags"].value
  write_flags = objects[WRITE_PATH]["org.bluez.GattCharacteristic1"]["Flags"].value
  assert "encrypt-authenticated-read" in notify_flags
  assert "encrypt-authenticated-write" in write_flags
  assert "write" not in notify_flags
  assert "notify" not in write_flags

  elm_flags = objects[ELM_CHARACTERISTIC_PATH]["org.bluez.GattCharacteristic1"]["Flags"].value
  assert "notify" in elm_flags
  assert "write-without-response" in elm_flags
  assert "encrypt-authenticated-read" in elm_flags
  assert "encrypt-authenticated-write" in elm_flags


def test_write_characteristic_routes_serial_response_to_notifications():
  snapshot = {
    "schemaVersion": 1,
    "updatedAt": 1000.0,
    "stateOfChargePercent": 50.0,
  }
  sessions = ObdSessionManager(VirtualObdVehicle(lambda: snapshot, clock=lambda: 1001.0))

  class FakeNotify:
    def __init__(self):
      self.calls = []

    async def send(self, payload, chunk_size):
      self.calls.append((payload, chunk_size))

  notify = FakeNotify()
  characteristic = ObdWriteCharacteristic(sessions, notify)
  asyncio.run(characteristic.handle_write(
    b"ATI\r",
    {"device": Variant("o", "/phone"), "mtu": Variant("q", 100)},
  ))
  assert notify.calls[0][0].startswith(b"ATI\r\nELM327")
  assert notify.calls[0][1] == 97


def test_notify_characteristic_sends_mtu_bounded_chunks():
  characteristic = ObdNotifyCharacteristic()
  characteristic.StartNotify()
  emitted = []
  characteristic.emit_properties_changed = lambda changed, _invalidated=[]: emitted.append(changed.get("Value"))
  asyncio.run(characteristic.send(b"abcdefghijklmnopqrstuvwxyz", chunk_size=10))
  assert emitted[-3:] == [b"abcdefghij", b"klmnopqrst", b"uvwxyz"]


def test_bluez_disconnect_signal_releases_the_single_obd_session():
  sessions = ObdSessionManager(VirtualObdVehicle(lambda: None))
  peripheral = BluezObdPeripheral(sessions)
  sessions.feed("/org/bluez/hci0/dev_PHONE_A", b"ATI\r")
  assert sessions.feed("/org/bluez/hci0/dev_PHONE_B", b"ATI\r") == b"BUSY\r>"

  peripheral._handle_bus_message(SimpleNamespace(
    message_type=MessageType.SIGNAL,
    interface="org.freedesktop.DBus.Properties",
    member="PropertiesChanged",
    path="/org/bluez/hci0/dev_PHONE_A",
    body=["org.bluez.Device1", {"Connected": Variant("b", False)}, []],
  ))
  assert b"ELM327" in sessions.feed("/org/bluez/hci0/dev_PHONE_B", b"ATI\r")


def test_bluez_pairing_signal_reports_success_during_owner_window():
  statuses = []
  passkeys = []
  peripheral = BluezObdPeripheral(
    ObdSessionManager(VirtualObdVehicle(lambda: None)),
    status_callback=statuses.append,
    passkey_callback=passkeys.append,
  )
  peripheral.pairing_window.open()
  peripheral._handle_bus_message(SimpleNamespace(
    message_type=MessageType.SIGNAL,
    interface="org.freedesktop.DBus.Properties",
    member="PropertiesChanged",
    path="/org/bluez/hci0/dev_PHONE_A",
    body=["org.bluez.Device1", {"Paired": Variant("b", True)}, []],
  ))
  assert statuses == ["paired"]
  assert passkeys == [""]
