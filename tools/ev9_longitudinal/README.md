# Kia EV9 resident preinit

This directory contains the receive-only capture tools and operating notes for the opt-in EV9 ADAS-ECU preinit path. The
feature lets resident Panda firmware reach the EV9's short startup diagnostic window before Linux boots, then hands
reconstruction and control to the normal host process.

The design is EV9-only and remains a vehicle-validation candidate. It is not a claim that OEM FCA/AEB or blind-spot
collision-avoidance braking remains available while the ADAS ECU is suppressed. See
[PREINIT_SAFETY_CASE.md](PREINIT_SAFETY_CASE.md) for the safety boundary and route evidence.

## Selection and firmware

The feature is disabled by default. `EV9LongPreinitPanda` is shown only when the persistent fingerprint is exactly
`KIA_EV9`; changing it requires explicit Panda-flash confirmation while offroad.

Build the two supported images:

```sh
scripts/laptop_device_build.sh scons panda_ev9_long_preinit_firmwares
```

The alias produces:

- `panda_h7_ev9_long_preinit.bin.signed` for normal EV9 starts;
- `panda_h7_ev9_long_preinit_hkg_remote.bin.signed` when HKG remote-climate/start support is also enabled.

Deploy both images together. There are no stage, probe, DTC, alternate-safety, or ignition-policy EV9 variants. The
resident path uses the existing `hyundaiCanfd` CCNC angle-long safety specialization; it does not add EV9 steering tuning
or a dedicated Panda safety model.

## Ownership sequence

| State | Owner and output |
|---|---|
| `COLLECTING` | Panda observes only. No diagnostic or replacement TX. |
| `WAIT_SESSION` | Panda sent `10 03` after exact EV9, stationary, and physical-start proof. |
| `WAIT_COMM_CONTROL` | Exact `50 03` chained `28 01 01`; replacement remains blocked. |
| `WAIT_SUPPRESSION` | Exact `68 01` arrived; Panda publishes neutral reconstruction while proving stock silence. |
| `ACTIVE` | Panda owns the managed set and sends Tester Present. Orange FCA and LKA show that host takeover is pending. |
| `HANDOFF` | The complete hardware-qualified host claim is one-way for this ignition epoch. Normal host/Panda safety behavior resumes; orange FCA remains and orange LKA clears. |
| `RESTORING` | At ignition OFF, replacement is quiesced and purged before `28 00 01`; exact `68 00` or fresh stock convergence proves release. |
| `ABORTED` | The attempt failed closed or restoration completed. A new knockout requires a new qualified start epoch. |

Knockout requires fresh four-wheel stationary proof and must finish before terminal READY. Host handoff intentionally
does not require Park, standstill, or zero speed: the driver may move after the stationary startup while the comma boots.
After `HANDOFF`, resident Panda does not resume neutral reconstruction for a host crash or ordinary safety rejection.
Host output stops through normal openpilot behavior, and firmware rearms only after the ignition-OFF release boundary.

## Identity and timing

Every wake epoch independently requires CRC-valid, masked EV9 physical bodies on bus 0 `0x100` and bus 1
`0x35`/`0xCB`, plus bus 1 `0xA0` with all four raw wheel speeds at or below 12. Door, lock, charging, and restored OFF
traffic are passive. A qualified physical ignition/start boundary supplies start intent.

The physical-start window is 300 ms. Each UDS request has a 50 ms P2 timeout and unresolved requests never overlap.
Panda requires exact `50 03` and `68 01` responses, then independently verifies that critical stock ADAS traffic is
quiet. Negative, malformed, late, or ambiguous responses fail closed.

The managed set is bus 0 `0x100` and bus 1 `0x12A`, `0xCB`, `0x160`, `0x161`, `0x162`, `0x1A0`, `0x1BA`, `0x1DA`,
`0x1E0`, `0x1E5`, `0x1EA`, `0x200`, `0x345`, and `0x38C`. Physical `0x57A` is never replayed. Frame address, bus,
length, cadence, fallback, neutralization, criticality, and slow-claim policy are defined in one firmware profile table.

## Host and cluster reconstruction

Before handoff, card verifies the resident epoch, exact safety configuration, current Panda faults, CAN health, a
complete returned neutral baseline, and hardware receipts for every managed tuple. Panda remains the final arbiter of
counter, CRC, cadence, and zero-actuation invariants until the complete claim reaches hardware.

After handoff the standard controller owns output. Dash reconstruction remains EV9-scoped:

- FCA and LKA ownership icons and lane-change animation are always reconstructed;
- `KiaEv9ClusterSideObjectsEnabled` gates qualified BSM reconstruction;
- `KiaEv9ClusterHeadwayEnabled` gates the stock-style speed-based headway line;
- `KiaEv9ClusterObjectsEnabled` gates primary and adjacent vehicle objects.

Invalid or stale radar/model inputs neutralize their display rather than freezing old objects. The grey steering icon
tracks AOL availability without changing lateral actuation authorization.

## Capturing and analysis

Use the bounded Cereal subscriber; do not open a second `Panda()` client during READY or preinit because Panda setup can
reinitialize CAN and disturb ownership.

```sh
python3 tools/ev9_longitudinal/capture_ev9_preinit_cereal.py /tmp/ev9-preinit.capnp --duration 600
python3 tools/ev9_longitudinal/analyze_ev9_preinit_capture.py /tmp/ev9-preinit.capnp \
  --route /data/media/0/realdata/<route>/<segment>/rlog.zst
```

The capture writes the event stream plus `.metadata.json`, `.stats.json`, and `.markers.jsonl`. The analyzer reports
diagnostic timing, ownership/status pages, managed CAN bodies, cluster icon state, and matching route logs.

## Required validation

Before wider road use, retain automatic route logs for cold power-on, direct and warm starts, remote climate, locked-fob
start, motion before handoff, READY-to-OFF release, LKA and gear transitions, overrides/cancel, host crash, queue clear,
unexpected stock reappearance, and CAN fault/overflow injection. Every run must show either an exact bounded handoff or a
fail-closed stock/neutral outcome, followed by a clean OFF release and next-cycle rearm.
