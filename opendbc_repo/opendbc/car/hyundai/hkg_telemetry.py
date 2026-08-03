from dataclasses import dataclass

from opendbc.can import CANParser
from opendbc.car import structs


ENERGY_MAX_AGE_NS = 500_000_000
ENERGY_MAX_SOURCE_SKEW_NS = 150_000_000


@dataclass(frozen=True)
class HKGEnergyTelemetry:
  available: bool = False
  soc_valid: bool = False
  dte_valid: bool = False
  charging_valid: bool = False
  charge_port_valid: bool = False
  fuel_gauge: float = 0.0
  distance_to_empty: float = 0.0
  charging: bool = False
  charging_port_connected: bool = False
  charging_time_remaining: float = 0.0
  source_mono_time: int = 0


def get_canfd_ev_energy_telemetry(cp: CANParser, *, require_redundant_soc: bool = False,
                                  enable_charging: bool = False,
                                  now_nanos: int | None = None) -> HKGEnergyTelemetry:
  if now_nanos is None:
    now_nanos = int(cp._last_update_nanos)

  def fresh(timestamp_nanos: int) -> bool:
    age_nanos = now_nanos - int(timestamp_nanos)
    return int(timestamp_nanos) > 0 and 0 <= age_nanos <= ENERGY_MAX_AGE_NS

  def aligned(first_timestamp_nanos: int, second_timestamp_nanos: int) -> bool:
    return abs(int(first_timestamp_nanos) - int(second_timestamp_nanos)) <= ENERGY_MAX_SOURCE_SKEW_NS

  display_soc = cp.vl["EV_ENERGY_STATUS_REDUNDANT"]["BATTERY_SOC_REDUNDANT"]
  display_soc_ts = cp.ts_nanos["EV_ENERGY_STATUS_REDUNDANT"]["BATTERY_SOC_REDUNDANT"]
  soc_valid = fresh(display_soc_ts) and 0.0 <= display_soc <= 100.0
  fuel_gauge = display_soc / 100.0 if soc_valid else 0.0
  soc_timestamps = [display_soc_ts] if soc_valid else []

  if require_redundant_soc:
    primary_soc = cp.vl["EV_ENERGY_STATUS"]["BATTERY_SOC"]
    primary_soc_ts = cp.ts_nanos["EV_ENERGY_STATUS"]["BATTERY_SOC"]
    soc_valid = (soc_valid and fresh(primary_soc_ts) and aligned(display_soc_ts, primary_soc_ts) and
                 0.0 <= primary_soc <= 100.0 and abs(display_soc - primary_soc) <= 1.0)
    fuel_gauge = (display_soc + primary_soc) / 200.0 if soc_valid else 0.0
    soc_timestamps = [display_soc_ts, primary_soc_ts] if soc_valid else []

  dte_km = cp.vl["EV_RANGE_STATUS"]["DISTANCE_TO_EMPTY"]
  dte_ts = cp.ts_nanos["EV_RANGE_STATUS"]["DISTANCE_TO_EMPTY"]
  dte_valid = fresh(dte_ts) and 0.0 < dte_km < 900.0
  distance_to_empty = dte_km * 1000.0 if dte_valid else 0.0

  charging_valid = False
  charge_port_valid = False
  charging = False
  charging_port_connected = False
  charging_timestamps = []
  if enable_charging:
    plug_connected = cp.vl["EV_CHARGE_STATUS"]["CHARGE_PORT_CONNECTED"] == 1
    plug_connected_redundant = cp.vl["EV_CHARGE_STATUS"]["CHARGE_PORT_CONNECTED_REDUNDANT"] == 1
    plug_ts = cp.ts_nanos["EV_CHARGE_STATUS"]["CHARGE_PORT_CONNECTED"]
    redundant_plug_ts = cp.ts_nanos["EV_CHARGE_STATUS"]["CHARGE_PORT_CONNECTED_REDUNDANT"]
    charge_port_valid = (fresh(plug_ts) and fresh(redundant_plug_ts) and aligned(plug_ts, redundant_plug_ts) and
                         plug_connected == plug_connected_redundant)
    charging_port_connected = charge_port_valid and plug_connected

    primary_charging = cp.vl["EV_ENERGY_STATUS"]["CHARGING_ACTIVE"] == 1
    redundant_charging = cp.vl["EV_CHARGE_STATUS"]["CHARGING_ACTIVE_REDUNDANT"] == 1
    primary_charging_ts = cp.ts_nanos["EV_ENERGY_STATUS"]["CHARGING_ACTIVE"]
    redundant_charging_ts = cp.ts_nanos["EV_CHARGE_STATUS"]["CHARGING_ACTIVE_REDUNDANT"]
    charging_valid = (charge_port_valid and fresh(primary_charging_ts) and fresh(redundant_charging_ts) and
                      aligned(primary_charging_ts, redundant_charging_ts) and
                      primary_charging == redundant_charging and
                      (not primary_charging or charging_port_connected))
    charging = charging_valid and primary_charging
    if charge_port_valid:
      charging_timestamps += [plug_ts, redundant_plug_ts]
    if charging_valid:
      charging_timestamps += [primary_charging_ts, redundant_charging_ts]

  source_timestamps = [*soc_timestamps, *charging_timestamps]
  if dte_valid:
    source_timestamps.append(dte_ts)

  return HKGEnergyTelemetry(
    available=soc_valid or dte_valid,
    soc_valid=soc_valid,
    dte_valid=dte_valid,
    charging_valid=charging_valid,
    charge_port_valid=charge_port_valid,
    fuel_gauge=fuel_gauge,
    distance_to_empty=distance_to_empty,
    charging=charging,
    charging_port_connected=charging_port_connected,
    source_mono_time=max(source_timestamps, default=0),
  )


def get_can_ev_cluster_dte(cp: CANParser) -> float:
  dte_km = cp.vl["CLU13"]["CF_Clu_DTE"]
  if cp.ts_nanos["CLU13"]["CF_Clu_DTE"] > 0 and 0.0 < dte_km < 900.0:
    return dte_km * 1000.0
  return 0.0


def populate_vehicle_telemetry(fp_ret, ret: structs.CarState, telemetry: HKGEnergyTelemetry) -> None:
  fp_ret.vehicleTelemetryAvailable = telemetry.available
  fp_ret.fuelGauge = ret.fuelGauge
  fp_ret.distanceToEmpty = ret.distanceToEmpty
  fp_ret.charging = ret.charging
  fp_ret.chargingPortConnected = ret.chargingPortConnected
  fp_ret.chargingTimeRemaining = ret.chargingTimeRemaining
  fp_ret.vehicleTelemetrySourceMonoTime = telemetry.source_mono_time
  fp_ret.vehicleTelemetrySocValid = telemetry.soc_valid
  fp_ret.vehicleTelemetryDteValid = telemetry.dte_valid
  fp_ret.vehicleTelemetryChargingValid = telemetry.charging_valid
  fp_ret.vehicleTelemetryChargePortValid = telemetry.charge_port_valid
  fp_ret.vEgo = ret.vEgo
  fp_ret.standstill = ret.standstill
