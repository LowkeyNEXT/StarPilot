# ruff: noqa: F722, F821
import asyncio
import os
from pathlib import Path

import pytest

from dbus_next import DBusError, Variant
from dbus_next.aio import MessageBus
from dbus_next.constants import BusType, PropertyAccess
from dbus_next.service import ServiceInterface, dbus_property, method

from openpilot.system.vehicle_telemetry.obd import ELM_UART_SERVICE_UUID, ObdSessionManager, VirtualObdVehicle
from openpilot.system.vehicle_telemetry.obd_bluez import (
  ADAPTER_PATH,
  ADVERTISEMENT_PATH,
  AGENT_PATH,
  APPLICATION_PATH,
  BLUEZ_SERVICE,
  ELM_CHARACTERISTIC_PATH,
  ELM_SERVICE_PATH,
  IFACE_OBJECT_MANAGER,
  NOTIFY_PATH,
  OBD_UART_SERVICE_UUID,
  SERVICE_PATH,
  WRITE_PATH,
  BluezObdPeripheral,
)


class FakeAdapter(ServiceInterface):
  def __init__(self):
    super().__init__("org.bluez.Adapter1")
    self.powered = False
    self.pairable = False
    self.discoverable = False
    self.alias = ""
    self.removed_devices = []

  @dbus_property(access=PropertyAccess.READWRITE)
  def Powered(self) -> "b":
    return self.powered

  @Powered.setter
  def Powered(self, value: "b"):
    self.powered = bool(value)

  @dbus_property(access=PropertyAccess.READWRITE)
  def Pairable(self) -> "b":
    return self.pairable

  @Pairable.setter
  def Pairable(self, value: "b"):
    self.pairable = bool(value)

  @dbus_property(access=PropertyAccess.READWRITE)
  def Discoverable(self) -> "b":
    return self.discoverable

  @Discoverable.setter
  def Discoverable(self, value: "b"):
    self.discoverable = bool(value)

  @dbus_property(access=PropertyAccess.READWRITE)
  def Alias(self) -> "s":
    return self.alias

  @Alias.setter
  def Alias(self, value: "s"):
    self.alias = str(value)

  @method()
  def RemoveDevice(self, device: "o"):
    self.removed_devices.append(device)


class FakeGattManager(ServiceInterface):
  def __init__(self):
    super().__init__("org.bluez.GattManager1")
    self.registered = []
    self.unregistered = []

  @method()
  def RegisterApplication(self, application: "o", options: "a{sv}"):
    self.registered.append((application, options))

  @method()
  def UnregisterApplication(self, application: "o"):
    self.unregistered.append(application)


class FakeAdvertisingManager(ServiceInterface):
  def __init__(self):
    super().__init__("org.bluez.LEAdvertisingManager1")
    self.registered = []
    self.unregistered = []

  @method()
  def RegisterAdvertisement(self, advertisement: "o", options: "a{sv}"):
    self.registered.append((advertisement, options))

  @method()
  def UnregisterAdvertisement(self, advertisement: "o"):
    self.unregistered.append(advertisement)


class FakeAgentManager(ServiceInterface):
  def __init__(self, adapter):
    super().__init__("org.bluez.AgentManager1")
    self.adapter = adapter
    self.registered = []
    self.default_agents = []
    self.unregistered = []

  @method()
  def RegisterAgent(self, agent: "o", capability: "s"):
    self.registered.append((agent, capability))

  @method()
  def RequestDefaultAgent(self, agent: "o"):
    self.default_agents.append(agent)
    # BlueZ 5.72 on the comma enables Pairable as a registration side effect.
    self.adapter.pairable = True

  @method()
  def UnregisterAgent(self, agent: "o"):
    self.unregistered.append(agent)


async def _run_real_dbus_lifecycle():
  server_bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
  await server_bus.request_name(BLUEZ_SERVICE)

  adapter = FakeAdapter()
  gatt = FakeGattManager()
  advertising = FakeAdvertisingManager()
  agents = FakeAgentManager(adapter)
  server_bus.export(ADAPTER_PATH, adapter)
  server_bus.export(ADAPTER_PATH, gatt)
  server_bus.export(ADAPTER_PATH, advertising)
  server_bus.export("/org/bluez", agents)
  snapshot = {
    "schemaVersion": 1,
    "updatedAt": 1000.0,
    "stateOfChargePercent": 50.0,
  }
  sessions = ObdSessionManager(VirtualObdVehicle(lambda: snapshot, clock=lambda: 1001.0))
  statuses = []
  passkeys = []
  peripheral = BluezObdPeripheral(
    sessions,
    status_callback=statuses.append,
    passkey_callback=passkeys.append,
  )

  try:
    await peripheral.start()
    assert adapter.powered
    assert adapter.alias == "CommaOBD"
    assert not adapter.pairable
    assert not adapter.discoverable
    assert gatt.registered == [(APPLICATION_PATH, {})]
    assert advertising.registered == [(ADVERTISEMENT_PATH, {})]
    assert agents.registered == [(AGENT_PATH, "DisplayOnly")]
    assert agents.default_agents == [AGENT_PATH]
    assert statuses[-1] == "ready"

    destination = peripheral.bus.unique_name
    application_intro = await server_bus.introspect(destination, APPLICATION_PATH)
    application = server_bus.get_proxy_object(destination, APPLICATION_PATH, application_intro)
    objects = await application.get_interface(IFACE_OBJECT_MANAGER).call_get_managed_objects()
    # dbus-next's standard ObjectManager also reports other exported children
    # below APPLICATION_PATH, while BlueZ's GATT manager selects the GATT
    # interfaces it understands from this tree.
    assert {SERVICE_PATH, NOTIFY_PATH, WRITE_PATH, ELM_SERVICE_PATH, ELM_CHARACTERISTIC_PATH}.issubset(objects)
    assert {ADVERTISEMENT_PATH, AGENT_PATH}.issubset(objects)
    assert objects[SERVICE_PATH]["org.bluez.GattService1"]["UUID"].value == OBD_UART_SERVICE_UUID
    assert objects[ELM_SERVICE_PATH]["org.bluez.GattService1"]["UUID"].value == ELM_UART_SERVICE_UUID

    await peripheral.set_pairing_window(True)
    assert adapter.pairable and adapter.discoverable

    agent_intro = await server_bus.introspect(destination, AGENT_PATH)
    agent = server_bus.get_proxy_object(destination, AGENT_PATH, agent_intro).get_interface("org.bluez.Agent1")
    await agent.call_display_passkey("/phone", 1234, 0)
    assert passkeys[-1] == "001234"
    assert statuses[-1] == "confirm-passkey"

    notify_intro = await server_bus.introspect(destination, NOTIFY_PATH)
    notify = server_bus.get_proxy_object(destination, NOTIFY_PATH, notify_intro).get_interface(
      "org.bluez.GattCharacteristic1"
    )
    await notify.call_start_notify()

    write_intro = await server_bus.introspect(destination, WRITE_PATH)
    write = server_bus.get_proxy_object(destination, WRITE_PATH, write_intro).get_interface(
      "org.bluez.GattCharacteristic1"
    )
    await write.call_write_value(
      b"ATI\r",
      {"device": Variant("o", "/phone"), "mtu": Variant("q", 100)},
    )
    assert peripheral.notify_characteristic._value.endswith(b">")
    assert b"ELM327" in peripheral.notify_characteristic._value

    elm_intro = await server_bus.introspect(destination, ELM_CHARACTERISTIC_PATH)
    elm = server_bus.get_proxy_object(destination, ELM_CHARACTERISTIC_PATH, elm_intro).get_interface(
      "org.bluez.GattCharacteristic1"
    )
    await elm.call_start_notify()
    await elm.call_write_value(
      b"ATI\r",
      {"device": Variant("o", "/phone"), "mtu": Variant("q", 100)},
    )
    assert peripheral.elm_characteristic._value.endswith(b">")
    assert b"ELM327" in peripheral.elm_characteristic._value

    await peripheral.set_pairing_window(False)
    assert not adapter.pairable and not adapter.discoverable
    with pytest.raises(DBusError):
      await agent.call_display_passkey("/phone", 4321, 0)
  finally:
    await peripheral.stop()
    server_bus.disconnect()

  assert advertising.unregistered == [ADVERTISEMENT_PATH]
  assert gatt.unregistered == [APPLICATION_PATH]
  assert agents.unregistered == [AGENT_PATH]
  assert statuses[-1] == "stopped"


def test_complete_bluez_lifecycle_over_real_dbus():
  system_bus_available = os.environ.get("DBUS_SYSTEM_BUS_ADDRESS") or any(
    path.exists() for path in (Path("/run/dbus/system_bus_socket"), Path("/var/run/dbus/system_bus_socket"))
  )
  if not system_bus_available:
    pytest.skip("system D-Bus is not available on this host")

  asyncio.run(_run_real_dbus_lifecycle())
