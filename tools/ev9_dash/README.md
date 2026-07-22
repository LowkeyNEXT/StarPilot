# Kia EV9 dash reconstruction

This branch reconstructs the EV9 CCNC dashboard scene from live openpilot state
without feeding any display-only decision back into planning or control.

## Production behavior

| Reconstruction | Default | Source and fail-closed behavior |
| --- | --- | --- |
| Center lead car | On while HDA is active | Fused, radar-backed `radarState.leadOne`; three 20 Hz acquisition samples, four-sample dropout/confidence hold, and a 0.35 distance EMA. Vision-only and nearest-raw fallbacks are rejected. |
| Target/headway line | On while HDA is active | A committed, valid StarPilot stop target overrides the stock EV9 `1.626 * vEgo` headway. Invalid/stale plans fall back to headway. |
| Speed-limit sign | On | Uses the shared EV6/Hyundai CAN-FD `FR_CMR_02_100ms` path and its existing ECAN/CAM bus selection. Raw values 1–253 pass through, warning maps to normal/red, and valid signs carry stock `COUNTRY=7`; 254/255 fail neutral. |
| Dynamic lane outlines | On while lateral control is active | Uses `modelV2.laneLineProbs[1:3]` for individual left/right visibility and the model's desired curvature for the shared CCNC curve selector. Visibility uses 0.55/0.45 hysteresis, curvature is bounded to 0.05 1/m and smoothed only on new model frames, and stale/invalid model state fails neutral. Stock positions (15/15) and zoom (1) are preserved. This is model reconstruction, not camera-CAN passthrough. |
| Mirror/dash BSM warning | On | Fresh native `0x1BA` remains authoritative. When it is stale, retained `0x36A` must coincide with a moving MRR35 track inside the adjacent-lane band; the matching physical `0x413` stalk escalates that detection to the flashing/audible warning. Missing/stale raw or radar input fails neutral. |
| Left/right scene cars | On while HDA is active | Strict MRR35 lifecycle, motion rejection, unambiguous acquisition, stable track retention, and the same 0.35 distance EMA. A fused track is promoted atomically between center and either adjacent slot so it cannot blink out or duplicate during a lane crossing. `KiaEv9ClusterSideObjectsEnabled=0` remains an emergency kill switch. |
| Second/rear side slots | Encoded, neutral without truth | Both left/right rear CCNC slots, their 8-bit range, output validation, and the side-object kill switch are implemented. Stock can populate front and rear simultaneously, but the retained buses do not expose a production-safe rear-occupancy decision, so runtime output fails neutral instead of inventing an object. |
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

When ADAS transmit suppression removes native `0x1BA`, the conservative raw
fallback provides limited-coverage warning output. It remains warning-output-only and
does not feed planning, lane-change policy, AEB, BCA, or longitudinal control.

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
the five differences occurred at asynchronous message transitions. The shared
camera path selects ECAN for LKA-steering cars such as the EV6/EV9 and CAM for
the other CAN-FD topology; only the EV9 CCNC encoder consumes the preserved raw
state, so other platforms keep their existing dashboard-speed behavior.

The 83,980 decoded stock `CCNC_0x161` samples establish the lane-message
baseline. Every d4-d6 frame kept both lane states neutral, both positions at
15, curvature at 15, and zoom at 1 even while the camera lane-curvature
signals changed. A separate stock route-15 configuration rendered white lane
outlines: across 1,199 samples aligned with `modelV2`, the left outline was
present in all 1,199 and the right in 1,114. The production visibility
hysteresis reproduced left presence at 100% precision/recall and right at
94.71% precision and 99.55% recall, with four output transitions. Because the
stock curve selector remained 15 throughout all route variants, dynamic
curvature cannot be claimed as a decoded OEM pass-through; it deliberately
uses the bounded, smoothed model desired curvature. Route alignment also
confirmed that model curvature uses the opposite sign from Hyundai steering,
which the encoder converts before reusing the existing shared CCNC mapping.

The d4-d6 BSM corpus contained 4,821 native left-lamp and 2,238 native right-lamp
samples. Retained `0x36A` alone measured only 23.8% precision/21.8% recall on
the left and 22.7%/30.9% on the right. Requiring an MRR35 target in the
adjacent-lane band (`2.5`–`4.0` m left or `2.5`–`4.5` m right) raised precision
to 79.3% left and 82.0% right with no acquisition delay. Three consecutive
right scans improved right precision to 87.3%; every tested dropout hold made
both sides less precise, so production uses no hold. Recall remains about 2.2%
because the forward MRR35 cannot see most rear blind-zone objects. The fallback
therefore prioritizes rejecting two-lanes-away proxies over coverage. Fresh
native corner-lamp state is intentionally not filtered and always wins.

Stock populated left front+rear simultaneously in 1,183 active samples and
right front+rear in 58. The full active corpus contained 3,569 left-rear and
1,475 right-rear samples. Those rear fields were usually fixed at 25 m, but
they did not faithfully follow native `0x1BA`, retained `0x36A`, a second MRR35
track, or any stable raw bit/value in the still-live `0x235`–`0x248` group.
Forward-radar second-slot inference measured only 12.4% presence precision and
created a 10-second false episode. An exhaustive fresh raw-byte/bit search had
best F1 of 0.48 left and 0.32 right; the few high-precision byte values changed
between d4 and d6 and therefore were object/route-specific rather than decoded
side-presence semantics. Rear output consequently remains neutral.

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

Score the exact production raw-BSM fallback separately:

```bash
./dev python tools/ev9_dash/score_stock_bsm.py \
  --stock '/path/to/stock-rlogs/000000d[456]--*/rlog.zst'
```

## Next recreations to validate

1. Provide native rear-corner/`0x1BA` visibility through hardware if BSM must
   survive full ADAS transmit suppression.
2. Validate both lane-animation directions on the vehicle with cluster video and fault logging
   before exposing its Param in UI.
3. Expose authoritative live rear/corner tracks, including lateral distance,
   through hardware or decode a cross-route-stable corner-radar object
   lifecycle. Use those tracks—not front-MRR coincidence—to constrain BSM to
   the adjacent lane and to enable the implemented rear scene slots.
4. Capture a new untouched active-HDA route, including close/rear side cars,
   plus night/poor-weather scenes for primary confidence.

Keep RCTA/rear-distance, `0x449`, `0x472`, BCA, and vibration fields neutral;
the retained signals do not support faithful reconstruction of them.
