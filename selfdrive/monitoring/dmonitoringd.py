#!/usr/bin/env python3
import cereal.messaging as messaging
from cereal import car
from openpilot.common.params import Params
from openpilot.common.realtime import config_realtime_process
from openpilot.common.swaglog import cloudlog
from openpilot.selfdrive.monitoring.policy import DriverMonitoring


def get_rhd_override(params):
  return params.get_bool("IsRHD") if params.get_bool("IsRHDOverride") else None


def dmonitoringd_thread():
  config_realtime_process([0, 1, 2, 3], 5)

  params = Params()
  CP = messaging.log_from_bytes(params.get("CarParams", block=True), car.CarParams)
  log_producer_health = str(CP.carFingerprint) == "KIA_EV9"
  pm = messaging.PubMaster(['driverMonitoringState'])
  sm = messaging.SubMaster(
    ['driverStateV2', 'liveCalibration', 'carState', 'selfdriveState', 'modelV2', 'starpilotCarState'],
    poll='driverStateV2',
    ignore_alive=['starpilotCarState'],
    ignore_avg_freq=['starpilotCarState'],
    ignore_valid=['starpilotCarState'],
  )

  DM = DriverMonitoring(
    rhd_saved=params.get_bool("IsRhdDetected"),
    always_on=params.get_bool("AlwaysOnDM"),
    rhd_override=get_rhd_override(params),
  )
  demo_mode=False
  valid_prev = True

  # 20Hz <- dmonitoringmodeld
  while True:
    sm.update()
    if not sm.updated['driverStateV2']:
      # iterate when model has new output
      continue

    valid = sm.all_checks()
    if log_producer_health and valid_prev and not valid:
      cloudlog.event("producerInvalid", producer="driverMonitoringState",
                     invalid=[s for s, service_valid in sm.valid.items() if not service_valid],
                     not_alive=[s for s, alive in sm.alive.items() if not alive],
                     not_freq_ok=[s for s, freq_ok in sm.freq_ok.items() if not freq_ok])
    valid_prev = valid
    if demo_mode and sm.valid['driverStateV2']:
      DM.run_step(sm, demo=True)
    elif valid:
      DM.run_step(sm, demo=demo_mode)

    # publish
    dat = DM.get_state_packet(valid=valid)
    pm.send('driverMonitoringState', dat)

    # load live always-on toggle
    if sm['driverStateV2'].frameId % 40 == 1:
      DM.always_on = params.get_bool("AlwaysOnDM")
      DM.wheel_on_right_default = params.get_bool("IsRhdDetected")
      DM.wheel_on_right_override = get_rhd_override(params)
      demo_mode = params.get_bool("IsDriverViewEnabled")

    # save rhd virtual toggle every 5 mins
    if (DM.wheel_on_right_override is None and sm['driverStateV2'].frameId % 6000 == 0 and not demo_mode and
     DM.wheelpos_offsetter.filtered_stat.n > DM.settings._WHEELPOS_FILTER_MIN_COUNT and
     DM.wheel_on_right == (DM.wheelpos_offsetter.filtered_stat.M > DM.settings._WHEELPOS_THRESHOLD)):
      params.put_bool("IsRhdDetected", DM.wheel_on_right)
      params.put_bool("IsRHD", DM.wheel_on_right)

def main():
  dmonitoringd_thread()


if __name__ == '__main__':
  main()
