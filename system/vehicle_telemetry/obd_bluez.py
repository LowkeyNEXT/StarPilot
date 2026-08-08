#!/usr/bin/env python3
# ruff: noqa: F722, F821, UP037
"""BlueZ D-Bus peripheral frontend for the read-only virtual OBD adapter."""

from __future__ import annotations

import asyncio
import time

from collections.abc import Callable

from dbus_next import BusType, DBusError, Message, Variant
from dbus_next.aio import MessageBus
from dbus_next.constants import MessageType, PropertyAccess
from dbus_next.service import ServiceInterface, dbus_property, method

from openpilot.system.vehicle_telemetry.obd import (
  DEFAULT_OBD_BLE_NAME,
  ELM_UART_CHARACTERISTIC_UUID,
  ELM_UART_SERVICE_UUID,
  OBD_UART_NOTIFY_UUID,
  OBD_UART_SERVICE_UUID,
  OBD_UART_SERVICE_UUIDS,
  OBD_UART_WRITE_UUID,
  ObdSessionManager,
  normalize_obd_ble_name,
)


BLUEZ_SERVICE = "org.bluez"
BLUEZ_ROOT_PATH = "/org/bluez"
BLUEZ_OBJECT_MANAGER_PATH = "/"
ADAPTER_PATH = "/org/bluez/hci0"
APPLICATION_PATH = "/org/starpilot/obd"
SERVICE_PATH = f"{APPLICATION_PATH}/service0"
NOTIFY_PATH = f"{SERVICE_PATH}/char0"
WRITE_PATH = f"{SERVICE_PATH}/char1"
ELM_SERVICE_PATH = f"{APPLICATION_PATH}/service1"
ELM_CHARACTERISTIC_PATH = f"{ELM_SERVICE_PATH}/char0"
ADVERTISEMENT_PATH = f"{APPLICATION_PATH}/advertisement0"
AGENT_PATH = f"{APPLICATION_PATH}/agent0"

PAIRING_WINDOW_SECONDS = 120
DEFAULT_NOTIFICATION_CHUNK_BYTES = 20

IFACE_OBJECT_MANAGER = "org.freedesktop.DBus.ObjectManager"
IFACE_PROPERTIES = "org.freedesktop.DBus.Properties"
IFACE_ADAPTER = "org.bluez.Adapter1"
IFACE_DEVICE = "org.bluez.Device1"
IFACE_GATT_MANAGER = "org.bluez.GattManager1"
IFACE_ADV_MANAGER = "org.bluez.LEAdvertisingManager1"
IFACE_AGENT_MANAGER = "org.bluez.AgentManager1"
DBUS_SERVICE = "org.freedesktop.DBus"
DBUS_PATH = "/org/freedesktop/DBus"
DBUS_INTERFACE = "org.freedesktop.DBus"

BLUEZ_SIGNAL_MATCH_RULES = (
  "type='signal',sender='org.bluez',interface='org.freedesktop.DBus.Properties'," +
  "member='PropertiesChanged',arg0='org.bluez.Device1'",
  "type='signal',sender='org.bluez',path='/',interface='org.freedesktop.DBus.ObjectManager'," +
  "member='InterfacesRemoved'",
)


class PairingWindow:
  def __init__(self, *, clock: Callable[[], float] = time.monotonic):
    self._clock = clock
    self._deadline = 0.0

  def open(self, seconds=PAIRING_WINDOW_SECONDS):
    seconds = max(1, min(PAIRING_WINDOW_SECONDS, int(seconds)))
    self._deadline = self._clock() + seconds
    return self._deadline

  def close(self):
    self._deadline = 0.0

  @property
  def active(self):
    return self._clock() < self._deadline

  @property
  def remaining_seconds(self):
    return max(0, int(self._deadline - self._clock() + 0.999)) if self.active else 0


class ObdObjectManager(ServiceInterface):
  def __init__(self, objects_provider):
    super().__init__(IFACE_OBJECT_MANAGER)
    self._objects_provider = objects_provider

  @method()
  def GetManagedObjects(self) -> "a{oa{sa{sv}}}":
    return self._objects_provider()


class ObdGattService(ServiceInterface):
  def __init__(self, uuid=OBD_UART_SERVICE_UUID):
    super().__init__("org.bluez.GattService1")
    self.uuid = uuid

  @dbus_property(access=PropertyAccess.READ)
  def UUID(self) -> "s":
    return self.uuid

  @dbus_property(access=PropertyAccess.READ)
  def Primary(self) -> "b":
    return True

  @dbus_property(access=PropertyAccess.READ)
  def Includes(self) -> "ao":
    return []


class ObdNotifyCharacteristic(ServiceInterface):
  FLAGS = ["read", "notify", "encrypt-authenticated-read"]

  def __init__(self):
    super().__init__("org.bluez.GattCharacteristic1")
    self._notifying = False
    self._value = b""

  @dbus_property(access=PropertyAccess.READ)
  def UUID(self) -> "s":
    return OBD_UART_NOTIFY_UUID

  @dbus_property(access=PropertyAccess.READ)
  def Service(self) -> "o":
    return SERVICE_PATH

  @dbus_property(access=PropertyAccess.READ)
  def Flags(self) -> "as":
    return list(self.FLAGS)

  @dbus_property(access=PropertyAccess.READ)
  def Notifying(self) -> "b":
    return self._notifying

  @dbus_property(access=PropertyAccess.READ)
  def Value(self) -> "ay":
    return self._value

  @method()
  def ReadValue(self, options: "a{sv}") -> "ay":
    return self._value

  @method()
  def StartNotify(self):
    if not self._notifying:
      self._notifying = True
      self.emit_properties_changed({"Notifying": True})

  @method()
  def StopNotify(self):
    if self._notifying:
      self._notifying = False
      self.emit_properties_changed({"Notifying": False})

  async def send(self, payload: bytes, chunk_size=DEFAULT_NOTIFICATION_CHUNK_BYTES):
    if not self._notifying or not payload:
      return
    chunk_size = max(1, min(244, int(chunk_size)))
    for offset in range(0, len(payload), chunk_size):
      self._value = bytes(payload[offset:offset + chunk_size])
      self.emit_properties_changed({"Value": self._value})
      await asyncio.sleep(0)


class ObdWriteCharacteristic(ServiceInterface):
  FLAGS = ["write", "write-without-response", "encrypt-authenticated-write"]

  def __init__(self, sessions: ObdSessionManager, notify_characteristic: ObdNotifyCharacteristic):
    super().__init__("org.bluez.GattCharacteristic1")
    self._sessions = sessions
    self._notify = notify_characteristic

  @dbus_property(access=PropertyAccess.READ)
  def UUID(self) -> "s":
    return OBD_UART_WRITE_UUID

  @dbus_property(access=PropertyAccess.READ)
  def Service(self) -> "o":
    return SERVICE_PATH

  @dbus_property(access=PropertyAccess.READ)
  def Flags(self) -> "as":
    return list(self.FLAGS)

  @method()
  async def WriteValue(self, value: "ay", options: "a{sv}"):
    await self.handle_write(value, options)

  async def handle_write(self, value, options):
    device = options.get("device", Variant("o", "/unknown")).value
    mtu = int(options.get("mtu", Variant("q", 23)).value)
    output = self._sessions.feed(str(device), bytes(value))
    await self._notify.send(output, max(1, mtu - 3))


class ElmUartCharacteristic(ServiceInterface):
  FLAGS = ["read", "notify", "write", "write-without-response", "encrypt-authenticated-read", "encrypt-authenticated-write"]

  def __init__(self, sessions: ObdSessionManager):
    super().__init__("org.bluez.GattCharacteristic1")
    self._sessions = sessions
    self._notifying = False
    self._value = b""

  @dbus_property(access=PropertyAccess.READ)
  def UUID(self) -> "s":
    return ELM_UART_CHARACTERISTIC_UUID

  @dbus_property(access=PropertyAccess.READ)
  def Service(self) -> "o":
    return ELM_SERVICE_PATH

  @dbus_property(access=PropertyAccess.READ)
  def Flags(self) -> "as":
    return list(self.FLAGS)

  @dbus_property(access=PropertyAccess.READ)
  def Notifying(self) -> "b":
    return self._notifying

  @dbus_property(access=PropertyAccess.READ)
  def Value(self) -> "ay":
    return self._value

  @method()
  def ReadValue(self, options: "a{sv}") -> "ay":
    return self._value

  @method()
  def StartNotify(self):
    if not self._notifying:
      self._notifying = True
      self.emit_properties_changed({"Notifying": True})

  @method()
  def StopNotify(self):
    if self._notifying:
      self._notifying = False
      self.emit_properties_changed({"Notifying": False})

  @method()
  async def WriteValue(self, value: "ay", options: "a{sv}"):
    device = options.get("device", Variant("o", "/unknown")).value
    mtu = int(options.get("mtu", Variant("q", 23)).value)
    output = self._sessions.feed(str(device), bytes(value))
    await self.send(output, max(1, mtu - 3))

  async def send(self, payload: bytes, chunk_size=DEFAULT_NOTIFICATION_CHUNK_BYTES):
    if not self._notifying or not payload:
      return
    chunk_size = max(1, min(244, int(chunk_size)))
    for offset in range(0, len(payload), chunk_size):
      self._value = bytes(payload[offset:offset + chunk_size])
      self.emit_properties_changed({"Value": self._value})
      await asyncio.sleep(0)


class ObdAdvertisement(ServiceInterface):
  def __init__(self, local_name=DEFAULT_OBD_BLE_NAME):
    super().__init__("org.bluez.LEAdvertisement1")
    self.local_name = normalize_obd_ble_name(local_name)

  @method()
  def Release(self):
    return

  @dbus_property(access=PropertyAccess.READ)
  def Type(self) -> "s":
    return "peripheral"

  @dbus_property(access=PropertyAccess.READ)
  def ServiceUUIDs(self) -> "as":
    return list(OBD_UART_SERVICE_UUIDS)

  @dbus_property(access=PropertyAccess.READ)
  def LocalName(self) -> "s":
    return self.local_name


class ObdPairingAgent(ServiceInterface):
  """Display-only standard BLE pairing agent; no application credentials."""

  def __init__(self, pairing_window: PairingWindow, *, passkey_callback=None, status_callback=None):
    super().__init__("org.bluez.Agent1")
    self._pairing_window = pairing_window
    self._passkey_callback = passkey_callback or (lambda _passkey: None)
    self._status_callback = status_callback or (lambda _state: None)

  def _require_window(self):
    if not self._pairing_window.active:
      raise DBusError("org.bluez.Error.Rejected", "The owner pairing window is closed")

  @method()
  def Release(self):
    self._status_callback("released")

  @method()
  def RequestPinCode(self, device: "o") -> "s":
    raise DBusError("org.bluez.Error.Rejected", "Legacy PIN pairing is disabled")

  @method()
  def DisplayPinCode(self, device: "o", pincode: "s"):
    raise DBusError("org.bluez.Error.Rejected", "Legacy PIN pairing is disabled")

  @method()
  def RequestPasskey(self, device: "o") -> "u":
    raise DBusError("org.bluez.Error.Rejected", "Keyboard pairing is disabled")

  @method()
  def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):
    self._require_window()
    self._passkey_callback(f"{int(passkey):06d}")
    self._status_callback("confirm-passkey")

  @method()
  def RequestConfirmation(self, device: "o", passkey: "u"):
    # DisplayOnly pairing should use passkey entry on the phone. Automatically
    # accepting numeric comparison would remove the human MITM check.
    raise DBusError("org.bluez.Error.Rejected", "Numeric comparison requires confirmation input")

  @method()
  def RequestAuthorization(self, device: "o"):
    self._require_window()

  @method()
  def AuthorizeService(self, device: "o", uuid: "s"):
    if str(uuid).lower() not in OBD_UART_SERVICE_UUIDS:
      raise DBusError("org.bluez.Error.Rejected", "Only the virtual OBD service is authorized")

  @method()
  def Cancel(self):
    self._passkey_callback("")
    self._status_callback("canceled")


class BluezObdPeripheral:
  def __init__(
    self,
    sessions: ObdSessionManager,
    *,
    passkey_callback=None,
    status_callback=None,
    pairing_window=None,
    local_name=DEFAULT_OBD_BLE_NAME,
  ):
    self.sessions = sessions
    self.passkey_callback = passkey_callback or (lambda _passkey: None)
    self.status_callback = status_callback or (lambda _status: None)
    self.pairing_window = pairing_window or PairingWindow()
    self.bus = None
    self.adapter_properties = None
    self.adapter = None
    self.gatt_manager = None
    self.advertising_manager = None
    self.agent_manager = None
    self.service = ObdGattService()
    self.elm_service = ObdGattService(ELM_UART_SERVICE_UUID)
    self.notify_characteristic = ObdNotifyCharacteristic()
    self.write_characteristic = ObdWriteCharacteristic(sessions, self.notify_characteristic)
    self.elm_characteristic = ElmUartCharacteristic(sessions)
    self.local_name = normalize_obd_ble_name(local_name)
    self.advertisement = ObdAdvertisement(self.local_name)
    self.agent = ObdPairingAgent(
      self.pairing_window,
      passkey_callback=self.passkey_callback,
      status_callback=self.status_callback,
    )
    self.object_manager = ObdObjectManager(self.managed_objects)
    self._matches_registered = False

  async def _set_signal_matches(self, member):
    for rule in BLUEZ_SIGNAL_MATCH_RULES:
      reply = await self.bus.call(Message(
        destination=DBUS_SERVICE,
        path=DBUS_PATH,
        interface=DBUS_INTERFACE,
        member=member,
        signature="s",
        body=[rule],
      ))
      if reply.message_type == MessageType.ERROR:
        detail = str(reply.body[0]) if reply.body else reply.error_name
        raise DBusError(reply.error_name or "org.freedesktop.DBus.Error.Failed", detail)

  def _handle_bus_message(self, message):
    if message.message_type != MessageType.SIGNAL:
      return None
    if message.interface == IFACE_PROPERTIES and message.member == "PropertiesChanged" and len(message.body) >= 2:
      interface, changed = message.body[:2]
      connected = changed.get("Connected") if interface == IFACE_DEVICE else None
      paired = changed.get("Paired") if interface == IFACE_DEVICE else None
      if paired is not None and bool(paired.value) and self.pairing_window.active:
        self.passkey_callback("")
        self.status_callback("paired")
      if connected is not None:
        if not bool(connected.value):
          self.sessions.disconnect(message.path)
    elif message.interface == IFACE_OBJECT_MANAGER and message.member == "InterfacesRemoved" and len(message.body) >= 2:
      path, interfaces = message.body[:2]
      if IFACE_DEVICE in interfaces:
        self.sessions.disconnect(path)
    return None

  @staticmethod
  def _service_properties(uuid=OBD_UART_SERVICE_UUID):
    return {
      "UUID": Variant("s", uuid),
      "Primary": Variant("b", True),
      "Includes": Variant("ao", []),
    }

  @staticmethod
  def _characteristic_properties(uuid, flags, service_path=SERVICE_PATH):
    return {
      "UUID": Variant("s", uuid),
      "Service": Variant("o", service_path),
      "Flags": Variant("as", list(flags)),
    }

  def managed_objects(self):
    return {
      SERVICE_PATH: {"org.bluez.GattService1": self._service_properties()},
      NOTIFY_PATH: {
        "org.bluez.GattCharacteristic1": self._characteristic_properties(OBD_UART_NOTIFY_UUID, ObdNotifyCharacteristic.FLAGS),
      },
      WRITE_PATH: {
        "org.bluez.GattCharacteristic1": self._characteristic_properties(OBD_UART_WRITE_UUID, ObdWriteCharacteristic.FLAGS),
      },
      ELM_SERVICE_PATH: {
        "org.bluez.GattService1": self._service_properties(ELM_UART_SERVICE_UUID),
      },
      ELM_CHARACTERISTIC_PATH: {
        "org.bluez.GattCharacteristic1": self._characteristic_properties(
          ELM_UART_CHARACTERISTIC_UUID, ElmUartCharacteristic.FLAGS, ELM_SERVICE_PATH,
        ),
      },
    }

  async def start(self):
    self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    await self._set_signal_matches("AddMatch")
    self._matches_registered = True
    self.bus.add_message_handler(self._handle_bus_message)
    adapter_intro = await self.bus.introspect(BLUEZ_SERVICE, ADAPTER_PATH)
    adapter_object = self.bus.get_proxy_object(BLUEZ_SERVICE, ADAPTER_PATH, adapter_intro)
    self.adapter_properties = adapter_object.get_interface(IFACE_PROPERTIES)
    self.adapter = adapter_object.get_interface(IFACE_ADAPTER)
    self.gatt_manager = adapter_object.get_interface(IFACE_GATT_MANAGER)
    self.advertising_manager = adapter_object.get_interface(IFACE_ADV_MANAGER)

    root_intro = await self.bus.introspect(BLUEZ_SERVICE, BLUEZ_ROOT_PATH)
    root_object = self.bus.get_proxy_object(BLUEZ_SERVICE, BLUEZ_ROOT_PATH, root_intro)
    self.agent_manager = root_object.get_interface(IFACE_AGENT_MANAGER)

    self.bus.export(APPLICATION_PATH, self.object_manager)
    self.bus.export(SERVICE_PATH, self.service)
    self.bus.export(NOTIFY_PATH, self.notify_characteristic)
    self.bus.export(WRITE_PATH, self.write_characteristic)
    self.bus.export(ELM_SERVICE_PATH, self.elm_service)
    self.bus.export(ELM_CHARACTERISTIC_PATH, self.elm_characteristic)
    self.bus.export(ADVERTISEMENT_PATH, self.advertisement)
    self.bus.export(AGENT_PATH, self.agent)

    await self.adapter_properties.call_set(IFACE_ADAPTER, "Powered", Variant("b", True))
    await self.adapter_properties.call_set(IFACE_ADAPTER, "Alias", Variant("s", self.local_name))
    await self.adapter_properties.call_set(IFACE_ADAPTER, "Pairable", Variant("b", False))
    await self.adapter_properties.call_set(IFACE_ADAPTER, "Discoverable", Variant("b", False))
    await self.agent_manager.call_register_agent(AGENT_PATH, "DisplayOnly")
    await self.agent_manager.call_request_default_agent(AGENT_PATH)
    await self.gatt_manager.call_register_application(APPLICATION_PATH, {})
    await self.advertising_manager.call_register_advertisement(ADVERTISEMENT_PATH, {})
    # Registering the default agent makes BlueZ 5.72 bondable/pairable on the
    # comma even when Pairable was cleared above. Reassert the closed pairing
    # window after every registration side effect has completed.
    await self.adapter_properties.call_set(IFACE_ADAPTER, "Pairable", Variant("b", False))
    await self.adapter_properties.call_set(IFACE_ADAPTER, "Discoverable", Variant("b", False))
    self.status_callback("ready")

  async def set_pairing_window(self, enabled, seconds=PAIRING_WINDOW_SECONDS):
    if self.adapter_properties is None:
      raise RuntimeError("BlueZ peripheral is not running")
    if enabled:
      self.pairing_window.open(seconds)
    else:
      self.pairing_window.close()
      self.passkey_callback("")
    await self.adapter_properties.call_set(IFACE_ADAPTER, "Pairable", Variant("b", bool(enabled)))
    await self.adapter_properties.call_set(IFACE_ADAPTER, "Discoverable", Variant("b", bool(enabled)))
    self.status_callback("pairing" if enabled else "ready")

  async def remove_bonded_devices(self):
    if self.bus is None or self.adapter is None:
      raise RuntimeError("BlueZ peripheral is not running")
    intro = await self.bus.introspect(BLUEZ_SERVICE, BLUEZ_OBJECT_MANAGER_PATH)
    root = self.bus.get_proxy_object(BLUEZ_SERVICE, BLUEZ_OBJECT_MANAGER_PATH, intro)
    objects = await root.get_interface(IFACE_OBJECT_MANAGER).call_get_managed_objects()
    for path, interfaces in objects.items():
      device = interfaces.get(IFACE_DEVICE)
      if device is not None and bool(device.get("Paired", Variant("b", False)).value):
        await self.adapter.call_remove_device(path)
    self.sessions.clear()
    self.status_callback("ready")

  async def stop(self):
    self.pairing_window.close()
    self.passkey_callback("")
    if self.adapter_properties is not None:
      try:
        await self.adapter_properties.call_set(IFACE_ADAPTER, "Pairable", Variant("b", False))
        await self.adapter_properties.call_set(IFACE_ADAPTER, "Discoverable", Variant("b", False))
      except Exception:
        pass
    if self.advertising_manager is not None:
      try:
        await self.advertising_manager.call_unregister_advertisement(ADVERTISEMENT_PATH)
      except Exception:
        pass
    if self.gatt_manager is not None:
      try:
        await self.gatt_manager.call_unregister_application(APPLICATION_PATH)
      except Exception:
        pass
    if self.agent_manager is not None:
      try:
        await self.agent_manager.call_unregister_agent(AGENT_PATH)
      except Exception:
        pass
    if self.bus is not None:
      self.bus.remove_message_handler(self._handle_bus_message)
      if self._matches_registered:
        try:
          await self._set_signal_matches("RemoveMatch")
        except Exception:
          pass
        self._matches_registered = False
      self.bus.disconnect()
    self.status_callback("stopped")
