# Kia EV9 dash reconstruction

This branch reconstructs the EV9 CCNC dashboard scene from live openpilot state
without feeding any display-only decision back into planning or control.

## Production behavior

| Reconstruction | Default | Source and fail-closed behavior |
| --- | --- | --- |
| Center lead car | On while HDA is active | Fused, radar-backed `radarState.leadOne`; three 20 Hz acquisition samples, four-sample dropout/confidence hold, and a 0.35 distance EMA. Vision-only and nearest-raw fallbacks are rejected. |
| Target/headway line | On while HDA is active | A committed, valid StarPilot stop target overrides the stock EV9 `1.626 * vEgo` headway. Invalid/stale plans fall back to headway. |
| Mirror/dash BSM warning | On when authoritative input is present | Fresh native `0x1BA` state plus the matching physical `0x413` stalk bit. Missing or older-than-100 ms native state clears the warning. The retained `0x36A` proxy is not used. |
| Left/right scene cars | Off | `KiaEv9ClusterSideObjectsEnabled=0`. Route 128 falsified the candidate classifier, so production output is suppressed even though the scorer retains shadow metrics. |
| Comma lane-change animation | Off | `KiaEv9ClusterLaneChangeAnimationEnabled=0`. Dynamic `0x3C1` synthesis preserves the live OEM body, follows the stock semantic counter, recomputes CRC, and is safety-allowlisted only on bus 1 with length 8. It still needs on-vehicle cluster/fault validation because the OEM sender remains live. |

The two disabled features are validation-only Params with no user-facing toggle.
They should be changed manually only for controlled testing, followed by a
restart of the car process. From `/data/openpilot` on a test device:

```bash
python3 -c 'from openpilot.common.params import Params; Params().put_bool("KiaEv9ClusterLaneChangeAnimationEnabled", True)'
python3 -c 'from openpilot.common.params import Params; Params().put_bool("KiaEv9ClusterSideObjectsEnabled", True)'
sudo reboot
```

Replace `True` with `False` to disable either gate. Side objects should remain
disabled until a new classifier passes an untouched route.

When ADAS transmit suppression removes native `0x1BA`, faithful BSM is not
available from the retained front/corner signals. Restoring it requires an
authoritative hardware-visible source or relay; the reconstruction deliberately
shows no warning instead of guessing.

## Route evidence

The preserved stock corpus contains 72 full rlogs from routes d4, d5, d6, and
route 15. Route 128 was used as a firmware/scenario-variance dataset during
refinement, so it is no longer an independent holdout.

Against the full d4-d6 corpus, the production primary selector measured 99.65%
precision and 89.99% recall for presence, 98.63%/89.07% within 5 m identity,
a 0.20 s maximum false-only episode, 0.115 m EMA MAE, 0.127 m EMA RMSE, 50.6%
second-difference jitter reduction, and about 84 ms empirical lag.

On route 128 it measured 96.23%/84.21% presence precision/recall,
96.19%/84.18% identity, a 0.75 s maximum false-only episode, 0.123 m EMA MAE,
0.149 m EMA RMSE, 75.1% jitter reduction, and about 91 ms lag. The experimental
side selector produced 41 false left frames in two episodes up to 1.55 s while
native CCNC showed no side car, which is why side production output is off.

The corpus is daytime. A new untouched night/poor-weather route is required to
validate the display-only model-confidence thresholds before calling primary
fidelity fully production-validated.

## Scoring

Run the exact production selector against route roots, individual segments, or
quoted globs:

```bash
./dev python tools/ev9_dash/score_stock_objects.py \
  --stock '/path/to/stock-rlogs/000000d[456]--*/rlog.zst' \
  --holdout '/path/to/route128/00000128--*--*/rlog.zst' \
  --verbose
```

Use `--json` for machine-readable results. Side output stays labeled as a
suppressed shadow unless `--experimental-side-output` is supplied.

## Next recreations to validate

1. Copy speed-limit display from `FR_CMR_02_100ms.ISLW_SpdCluMainDis`, treating
   254 and 255 as invalid.
2. Provide native rear-corner/`0x1BA` visibility through hardware if BSM must
   survive full ADAS transmit suppression.
3. Validate lane animation on the vehicle with cluster video and fault logging
   before exposing its Param in UI.
4. Capture a new untouched night/poor-weather route for primary confidence.

Keep RCTA/rear-distance, `0x449`, `0x472`, BCA, and vibration fields neutral;
the retained signals do not support faithful reconstruction of them.
