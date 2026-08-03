#!/usr/bin/env python3
# simple pandad wrapper that updates the panda first
import os
import usb1
import time
import signal
import subprocess

from panda import Panda, PandaDFU, PandaProtocolMismatch, FW_PATH
from openpilot.common.basedir import BASEDIR
from openpilot.common.params import Params, UnknownKeyName
from openpilot.system.hardware import HARDWARE
from openpilot.selfdrive.pandad import ev9_preinit
from openpilot.common.swaglog import cloudlog

def get_selected_firmware_name(app_fn: str, remote_start: bool, hkg_remote_start: bool,
                               ignore_ignition_line: bool, ev9_long_preinit: bool = False) -> str:
  h7 = app_fn == "panda_h7.bin.signed"
  if ev9_long_preinit and h7:
    return ev9_preinit.firmware_name(hkg_remote_start)
  if not remote_start and not hkg_remote_start and not ignore_ignition_line:
    return app_fn

  name_parts = ["panda_h7" if h7 else "panda"]
  if hkg_remote_start:
    name_parts.extend(["hkg", "remote"])
  elif remote_start:
    name_parts.append("remote")
  if ignore_ignition_line:
    name_parts.append("can_ignition_only")
  return "_".join(name_parts) + ".bin.signed"


def get_expected_firmware_path(panda: Panda, remote_start: bool, hkg_remote_start: bool,
                               ignore_ignition_line: bool, ev9_long_preinit: bool = False) -> str:
  app_fn = panda.get_mcu_type().config.app_fn
  selected_fn = get_selected_firmware_name(
    app_fn, remote_start, hkg_remote_start, ignore_ignition_line, ev9_long_preinit,
  )
  if selected_fn != app_fn:
    selected_path = os.path.join(FW_PATH, selected_fn)
    if os.path.isfile(selected_path):
      return selected_path
    if ev9_long_preinit and app_fn == ev9_preinit.H7_APP:
      raise FileNotFoundError(f"Selected EV9 Panda preinit firmware not found: {selected_path}")
    cloudlog.warning(f"Selected panda firmware not found: {selected_path}, falling back to default")
  return os.path.join(FW_PATH, app_fn)


def get_expected_signature(panda: Panda, remote_start: bool, hkg_remote_start: bool,
                           ignore_ignition_line: bool, ev9_long_preinit: bool = False) -> bytes:
  try:
    fn = get_expected_firmware_path(
      panda, remote_start, hkg_remote_start, ignore_ignition_line, ev9_long_preinit,
    )
    return Panda.get_signature_from_firmware(fn)
  except Exception:
    cloudlog.exception("Error computing expected signature")
    return b""


def get_remote_start_boots_comma(params: Params) -> bool:
  try:
    return params.get_bool("RemoteStartBootsComma")
  except UnknownKeyName:
    return False


def get_hkg_remote_start_boots_comma(params: Params) -> bool:
  try:
    return params.get_bool("HKGRemoteStartBootsComma")
  except UnknownKeyName:
    return False


def get_ignore_ignition_line(params: Params) -> bool:
  try:
    return params.get_bool("IgnoreIgnitionLine")
  except UnknownKeyName:
    return False


def flash_panda(panda_serial: str, remote_start: bool, hkg_remote_start: bool,
                ignore_ignition_line: bool, ev9_long_preinit: bool = False,
                preinit_recovery_blocked: bool = False) -> Panda:
  try:
    panda = Panda(panda_serial)
  except PandaProtocolMismatch:
    if ev9_long_preinit or preinit_recovery_blocked:
      cloudlog.error("Panda protocol mismatch with EV9 preinit armed/latched; refusing hardware recovery")
    else:
      cloudlog.warning("detected protocol mismatch, reflashing panda")
      HARDWARE.recover_internal_panda()
    raise

  internal_panda = panda.is_internal()
  preinit_status = None if panda.bootstub or not internal_panda else ev9_preinit.get_status(panda)
  if ev9_preinit.must_preserve(preinit_status):
    cloudlog.warning(f"Preserving in-flight EV9 Panda preinit firmware on {panda_serial}: {preinit_status}")
    return panda

  preinit_firmware_selected = ev9_preinit.firmware_selected(panda, ev9_long_preinit)
  if preinit_firmware_selected:
    fw_path = ev9_preinit.firmware_path(panda, hkg_remote_start)
    fw_signature = ev9_preinit.expected_signature(panda, hkg_remote_start)
  else:
    fw_path = get_expected_firmware_path(panda, remote_start, hkg_remote_start, ignore_ignition_line)
    fw_signature = get_expected_signature(panda, remote_start, hkg_remote_start, ignore_ignition_line)

  panda_version = "bootstub" if panda.bootstub else panda.get_version()
  panda_signature = b"" if panda.bootstub else panda.get_signature()
  resident_preinit_firmware = ev9_preinit.resident_signature(panda_signature)
  cloudlog.warning(f"Panda {panda_serial} connected, version: {panda_version}, signature {panda_signature.hex()[:16]}, expected {fw_signature.hex()[:16]}")

  if panda.bootstub or panda_signature != fw_signature:
    # Status can advance while signatures are read from disk. Recheck at the
    # last possible point before a firmware mutation.
    status_before_health = None if panda.bootstub or not (internal_panda or resident_preinit_firmware) else \
      ev9_preinit.get_status(panda)
    preinit_sensitive = (preinit_firmware_selected or resident_preinit_firmware or
                         ev9_preinit.status_valid(status_before_health))
    ignition_on = ev9_preinit.ignition_on(panda) if preinit_sensitive and not panda.bootstub else False
    status_after_health = ev9_preinit.get_status(panda) if preinit_sensitive and not panda.bootstub else status_before_health
    status_stable = ev9_preinit.status_stable(status_before_health, status_after_health)
    preinit_status = status_after_health if ev9_preinit.status_valid(status_after_health) else status_before_health
    if ev9_preinit.flash_blocked(preinit_status, preinit_firmware_selected, ignition_on,
                                 resident_preinit_firmware, status_stable):
      if preinit_sensitive and ignition_on and not ev9_preinit.must_preserve(preinit_status):
        cloudlog.error(f"Refusing to change resident EV9 Panda preinit firmware while ignition is on: {panda_serial}")
        return panda
      cloudlog.warning(f"Preserving in-flight EV9 Panda preinit firmware on {panda_serial}: {preinit_status}")
      return panda
    cloudlog.info("Panda firmware out of date, update required")
    panda.flash(fn=fw_path)
    cloudlog.info("Done flashing")

  if panda.bootstub:
    bootstub_version = panda.get_version()
    cloudlog.info(f"Flashed firmware not booting, flashing development bootloader. {bootstub_version=}, {internal_panda=}")
    if internal_panda:
      HARDWARE.recover_internal_panda()
    panda.recover(reset=(not internal_panda))
    cloudlog.info("Done flashing bootstub")

  if panda.bootstub:
    cloudlog.info("Panda still not booting, exiting")
    raise AssertionError

  panda_signature = panda.get_signature()
  if panda_signature != fw_signature:
    cloudlog.info("Version mismatch after flashing, exiting")
    raise AssertionError

  return panda


def main() -> None:
  # signal pandad to close the relay and exit
  def signal_handler(signum, frame):
    cloudlog.info(f"Caught signal {signum}, exiting")
    nonlocal do_exit
    do_exit = True
    if process is not None:
      process.send_signal(signal.SIGINT)

  process = None
  do_exit = False
  signal.signal(signal.SIGINT, signal_handler)

  count = 0
  first_run = True
  params = Params()
  no_internal_panda_count = 0
  preinit_resident_latched = False

  while not do_exit:
    try:
      count += 1
      cloudlog.event("pandad.flash_and_connect", count=count)
      params.remove("PandaSignatures")
      ev9_long_preinit = ev9_preinit.enabled_from_params(params)
      preinit_resident_latched |= ev9_long_preinit

      # Handle missing internal panda
      if no_internal_panda_count > 0:
        if preinit_resident_latched:
          cloudlog.error("Panda missing with resident EV9 preinit latched; refusing hardware reset/recovery")
        elif no_internal_panda_count == 3:
          cloudlog.info("No pandas found, putting internal panda into DFU")
          HARDWARE.recover_internal_panda()
        else:
          cloudlog.info("No pandas found, resetting internal panda")
          HARDWARE.reset_internal_panda()
        time.sleep(3)  # wait to come back up

      # Flash all Pandas in DFU mode
      dfu_serials = PandaDFU.list()
      if len(dfu_serials) > 0 and preinit_resident_latched:
        cloudlog.error("Panda in DFU with resident EV9 preinit latched; refusing automatic recovery")
        time.sleep(1)
      elif len(dfu_serials) > 0:
        for serial in dfu_serials:
          cloudlog.info(f"Panda in DFU mode found, flashing recovery {serial}")
          PandaDFU(serial).recover()
        time.sleep(1)

      panda_serials = Panda.list()
      if len(panda_serials) == 0:
        no_internal_panda_count += 1
        continue

      cloudlog.info(f"{len(panda_serials)} panda(s) found, connecting - {panda_serials}")

      # Flash pandas
      pandas: list[Panda] = []
      remote_start = get_remote_start_boots_comma(params)
      hkg_remote_start = get_hkg_remote_start_boots_comma(params)
      ignore_ignition_line = get_ignore_ignition_line(params)
      for serial in panda_serials:
        pandas.append(flash_panda(serial, remote_start, hkg_remote_start, ignore_ignition_line,
                                  ev9_long_preinit, preinit_resident_latched))

      # Ensure internal panda is present if expected
      internal_pandas = [panda for panda in pandas if panda.is_internal()]
      if HARDWARE.has_internal_panda() and len(internal_pandas) == 0:
        cloudlog.error("Internal panda is missing, trying again")
        no_internal_panda_count += 1
        continue
      no_internal_panda_count = 0

      # sort pandas to have deterministic order
      # * the internal one is always first
      # * then sort by hardware type
      # * as a last resort, sort by serial number
      pandas.sort(key=lambda x: (not x.is_internal(), x.get_type(), x.get_usb_serial()))
      panda_serials = [p.get_usb_serial() for p in pandas]

      # log panda fw versions
      params.put("PandaSignatures", b','.join(p.get_signature() for p in pandas))

      resident_ev9_long_preinit = False
      ev9_preinit_serials = []
      for panda in pandas:
        # check health for lost heartbeat
        health = panda.health()
        if health["heartbeat_lost"]:
          params.put_bool("PandaHeartbeatLost", True)
          cloudlog.event("heartbeat lost", deviceState=health, serial=panda.get_usb_serial())
        if health["som_reset_triggered"]:
          params.put_bool("PandaSomResetTriggered", True)
          cloudlog.event("panda.som_reset_triggered", health=health, serial=panda.get_usb_serial())

        preinit_firmware_selected = ev9_preinit.firmware_selected(panda, ev9_long_preinit)
        resident_preinit_firmware = ev9_preinit.resident_signature(panda.get_signature())
        preinit_status = ev9_preinit.get_status(panda) if panda.is_internal() or resident_preinit_firmware else None
        resident_status = ev9_preinit.status_valid(preinit_status)
        resident_ev9_long_preinit |= resident_status or resident_preinit_firmware
        if resident_status or resident_preinit_firmware:
          ev9_preinit_serials.append(panda.get_usb_serial())

        preinit_sensitive = resident_status or resident_preinit_firmware or preinit_firmware_selected
        verified_status = ev9_preinit.get_status(panda) if first_run and preinit_sensitive else preinit_status
        status_stable = ev9_preinit.status_stable(preinit_status, verified_status)
        preinit_status = verified_status if ev9_preinit.status_valid(verified_status) else preinit_status
        verified_ignition_on = ev9_preinit.ignition_on(panda) if first_run and preinit_sensitive else bool(
          health["ignition_line"] or health["ignition_can"]
        )
        preserve_preinit = ev9_preinit.reset_blocked(
          preinit_status, preinit_firmware_selected, resident_preinit_firmware,
          verified_ignition_on, status_stable,
        )
        if first_run and preserve_preinit:
          cloudlog.warning(f"Preserving EV9 Panda preinit state on {panda.get_usb_serial()}: {preinit_status}")
        elif first_run:
          # reset panda to ensure we're in a good state
          cloudlog.info(f"Resetting panda {panda.get_usb_serial()}")
          panda.reset(reconnect=True)

      preinit_resident_latched |= bool(ev9_preinit_serials)

      for p in pandas:
        p.close()
    # TODO: wrap all panda exceptions in a base panda exception
    except (usb1.USBErrorNoDevice, usb1.USBErrorPipe):
      # a panda was disconnected while setting everything up. let's try again
      cloudlog.exception("Panda USB exception while setting up")
      continue
    except PandaProtocolMismatch:
      cloudlog.exception("pandad.protocol_mismatch")
      continue
    except Exception:
      cloudlog.exception("pandad.uncaught_exception")
      continue

    first_run = False

    # run pandad with all connected serials as arguments
    run_ev9_long_preinit = ev9_long_preinit or resident_ev9_long_preinit
    if (get_remote_start_boots_comma(params) or get_hkg_remote_start_boots_comma(params) or
        get_ignore_ignition_line(params) or run_ev9_long_preinit):
      os.environ["BOARDD_SKIP_FW_CHECK"] = "1"
    else:
      os.environ.pop("BOARDD_SKIP_FW_CHECK", None)
    if ev9_preinit_serials:
      os.environ["BOARDD_EV9_LONG_PREINIT_SERIALS"] = ",".join(ev9_preinit_serials)
    else:
      os.environ.pop("BOARDD_EV9_LONG_PREINIT_SERIALS", None)
    os.environ['MANAGER_DAEMON'] = 'pandad'
    process = subprocess.Popen(["./pandad", *panda_serials], cwd=os.path.join(BASEDIR, "selfdrive/pandad"))
    process.wait()


if __name__ == "__main__":
  main()
