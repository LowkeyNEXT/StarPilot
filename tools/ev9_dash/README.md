# Kia EV9 dash reconstruction

This branch reconstructs the EV9 CCNC dashboard scene from live openpilot state
without feeding any display-only decision back into planning or control.

## Production behavior

| Reconstruction | Default | Source and fail-closed behavior |
| --- | --- | --- |
| Center lead car | On while HDA is active | Fused, radar-backed `radarState.leadOne`; three 20 Hz acquisition samples, four-sample dropout/confidence hold, and a 0.35 distance EMA. Vision-only and nearest-raw fallbacks are rejected. |
| Target/headway line | On while HDA is active | A committed, valid StarPilot stop target overrides the stock EV9 `1.626 * vEgo` headway. Invalid/stale plans fall back to headway. |
| Speed-limit sign | On | Reuses the generic Hyundai `FR_CMR_02_100ms` decode, copies raw values 1–253, and maps the stock warning state to normal/red CCNC sign color. Values 254/255 fail neutral. |
| Mirror/dash BSM warning | On when authoritative input is present | Fresh native `0x1BA` state plus the matching physical `0x413` stalk bit. Missing or older-than-100 ms native state clears the warning. The retained `0x36A` proxy is not used. |
| Left/right scene cars | On while HDA is active | Strict MRR35 lifecycle, motion rejection, unambiguous acquisition, stable track retention, and the same 0.35 distance EMA. `KiaEv9ClusterSideObjectsEnabled=0` remains an emergency kill switch. |
| Comma lane-change animation | Off | Both left and right are implemented. Dynamic `0x3C1` synthesis preserves the live OEM body, follows the stock semantic counter, recomputes CRC, and is safety-allowlisted only on bus 1 with length 8. It still needs on-vehicle cluster/fault validation because the OEM sender remains live. |

The lane animation and side-object kill switch have no user-facing toggle.
Change them manually only for controlled testing, followed by a restart of the
car process. From `/data/openpilot` on a test device:

```bash
python3 -c 'from openpilot.common.params import Params; Params().put_bool("KiaEv9ClusterLaneChangeAnimationEnabled", True)'
python3 -c 'from openpilot.common.params import Params; Params().put_bool("KiaEv9ClusterSideObjectsEnabled", False)'
sudo reboot
```

Reverse either Boolean to restore its default state.

When ADAS transmit suppression removes native `0x1BA`, faithful BSM is not
available from the retained front/corner signals. Restoring it requires an
authoritative hardware-visible source or relay; the reconstruction deliberately
shows no warning instead of guessing.

## Route evidence

The preserved stock corpus contains 72 full rlogs from routes d4, d5, d6, and
route 15. Route 128 was used as a firmware/scenario-variance dataset during
refinement, so it is no longer an independent holdout.

Across all 72 preserved logs in the production-equivalent `TARGET=3`,
`HDA_ICON=2` envelope, the primary selector measured 99.66% precision and
90.22% recall, with 98.65%/89.31% within-5-m identity, 0.116 m EMA MAE, 51.5%
jitter reduction, and about 85 ms empirical lag.

Left side presence measured 91.31% precision and 86.19% recall; correctly
associated ranges had 0.090 m EMA MAE. Right side presence and identity measured
97.23% precision and 68.93% recall, with 0.139 m EMA MAE and 71.6% jitter
reduction. The selector deliberately suppresses ambiguous acquisitions rather
than jumping between adjacent tracks. Three stock left-only episodes remained,
with a maximum duration of 1.85 s; close/rear side slots without a trustworthy
front-radar range remain a known limitation.

Route 128 contains no `HDA_ICON=2` frames, so it is not an active-output side
holdout. Its previously reported 41 false left frames were standby raw tracks
that the production encoder cannot display. A new untouched active-HDA route is
still required as a true side-output holdout.

The speed-limit comparison copied 7,512 of 7,517 valid stock samples exactly;
the five differences occurred at asynchronous message transitions.

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

Use `--json` for machine-readable results. Use `--suppress-side-output` to
score the persistent side-object kill-switch behavior.

## Next recreations to validate

1. Provide native rear-corner/`0x1BA` visibility through hardware if BSM must
   survive full ADAS transmit suppression.
2. Validate both lane-animation directions on the vehicle with cluster video and fault logging
   before exposing its Param in UI.
3. Capture a new untouched active-HDA route, including close/rear side cars,
   plus night/poor-weather scenes for primary confidence.

Keep RCTA/rear-distance, `0x449`, `0x472`, BCA, and vibration fields neutral;
the retained signals do not support faithful reconstruction of them.
