# EV9 resident preinit safety case

Status: production candidate with parked validation of direct cold/warm starts, remote-climate entry, and locked-fob
remote start. This document defines the intended safety boundary. It is not an ISO certification, ASIL argument, FMEDA,
or claim that OEM ADAS functions remain available.

## Scope and assumptions

Host selection requires explicit `EV9LongPreinitPanda` opt-in, exact cached `KIA_EV9` identity with ADAS ECU `0x730`,
openpilot longitudinal, the expected CCNC flags, and an internal H7 Panda. Resident firmware does not trust that cache:
every attempt requires current-cycle CRC-valid masked physical identity and fresh stationary proof.

Only two signed firmware combinations are supported: EV9 preinit, and EV9 preinit with HKG remote-start/climate support.
Both use the existing Panda `hyundaiCanfd` CCNC angle-long profile. The required safety parameter is `0x8495`; cached
StarPilot safety may add only AOL bit `0x0800`. Other flags, vehicles, and safety models cannot inherit ownership.

## Safety goals

| Hazard | Prevention or containment |
|---|---|
| Diagnostic TX on another vehicle | Dedicated firmware selection plus current-cycle EV9 body masks, exact bus/address/length tuples, and no generic identity fallback. |
| Knockout while moving | Fresh `0xA0` four-wheel stationary proof is checked immediately before both `10 03` and `28 01 01`. |
| Late or ambiguous ownership | Terminal READY veto, 300 ms start deadline, independent 50 ms P2 deadlines, one unresolved request maximum, exact positive responses, and independent stock-silence proof. |
| Replacement before suppression | Internal bridge TX is blocked until exact `68 01`; all permitted resident bodies are neutral and pass a final allowlist immediately before enqueue. |
| Duplicate publishers | Panda owns per-tuple cadence through the complete hardware-qualified claim. `HANDOFF` is one-way and resident publication stops. Unexpected stock reappearance quiesces replacement. |
| Unsafe host claim | Card requires exact resident status, coherent timing pages, expected safety, current-fault clearance, stable CAN health, a complete neutral baseline, and returned hardware receipts. |
| Host crash after handoff | Resident reconstruction does not restart. Standard process liveness, alerts, and Panda safety stop host output or actuation. |
| Unsafe restoration | Normal restoration is OFF-only: quiesce and purge first, send `28 00 01`, then require exact `68 00` or complete fresh critical-stock convergence. |
| Stale epoch reused | Ignition OFF latches the old epoch, clears software/hardware ownership, releases stock, and requires a new physical start epoch before rearming. |
| Misleading display | Panda ownership shows orange FCA/LKA; host ownership keeps orange FCA and clears orange LKA. Invalid object/lane inputs neutralize. |

## Diagnostic and transmit boundaries

Resident diagnostic TX is limited to:

- `10 03` extended session;
- `28 01 01` reception enabled / normal transmission disabled;
- `3e 80` response-suppressed Tester Present;
- `28 00 01` normal transmission restore;
- `10 01` default-session cleanup.

Production does not read or clear DTCs and does not originate diagnostics to the camera, cluster, radar, or EPS. The host
runtime safety surface permits only Tester Present.

The resident managed set is bus 0 `0x100` and bus 1 `0x12A`, `0xCB`, `0x160`, `0x161`, `0x162`, `0x1A0`, `0x1BA`,
`0x1DA`, `0x1E0`, `0x1E5`, `0x1EA`, `0x200`, `0x345`, and `0x38C`. Physical `0x57A` is not suppressed or replayed.
The final resident gate enforces inactive `0xCB`, zero assist/torque on `0x12A`, and neutral acceleration/emergency
fields on `0x1A0`. The existing Hyundai safety hook independently applies steering-angle, acceleration, brake, gas,
gear, cruise, heartbeat, and RX checks.

Knockout is stationary-only. Handoff is allowed in Park or motion and does not weaken the existing CCNC movement/gear
actuation gates. Counter/CRC continuity joins the frozen claim epoch to normal 100 Hz host output without creating a
second message epoch.

## Failure behavior

| Condition | Required result |
|---|---|
| Identity, start-intent, stationary, timing, or UDS failure before suppression | Abort; send no replacement and leave stock authoritative. |
| Exact suppression with host unavailable | Panda publishes only neutral reconstruction and orange ownership warnings. |
| Complete healthy host claim | Enter one-way `HANDOFF`; normal host output and Panda safety own the epoch. |
| Ordinary safety rejection, host process error, or queue clear after handoff | Do not infer ownership loss and do not restart Panda reconstruction; use normal openpilot fault behavior. |
| Current Panda fault, permanent fault status, CAN error, or unbounded claim health change | Fail closed and inhibit actuation. An empty historical temporary status is not a current fault. |
| Ignition OFF | Stop host/resident replacement, purge pending TX, prove stock restoration, clean the diagnostic session, and rearm. |
| Reset while suppression may be active | Enter restore-only recovery; never assume stock ownership from lost RAM state. |

OEM FCA/AEB and blind-spot collision-avoidance braking are unavailable while normal ADAS transmission is disabled.
Reconstructed icons, objects, or live radar do not imply those functions are present. The driver remains responsible for
steering, braking, and canceling.

## Representative evidence

Successful paths:

| Path | Route | Key result |
|---|---|---|
| Direct OFF-to-READY | `00000187--2d276303a9` | Exact responses, neutral warnings, complete handoff. |
| Cold comma boot | `0000018c--7b28efa2e9` | Resident knockout before Linux; later host claim. |
| Immediate warm restart | `0000018e--1f8d2b1cbd` | Fresh epoch and clean reknockout. |
| Remote-climate entry | `00000193--484f0d3839` | Climate-to-READY trigger qualified. |
| Locked-fob remote start | `0000019d--6cc955b36b` | Late wake still completed within the bounded start window. |

Key failures that define the final architecture:

| Evidence | Requirement learned |
|---|---|
| 144/171/172 | No post-READY request, no overlapping UDS, and no bridge before exact `68 01`. |
| 177/17b | Phase-walking retries and hardware receipts are required for slow managed streams. |
| 180 | Deploy both selectable firmware images as one versioned set. |
| 181 | Physical identity must never be learned from a returned replacement body. |
| 182-185 | Direct RX response chaining and stable NOOUTPUT are required to survive early-boot load and LED-loop delay. |
| 187/189/18a/1a4/1a6/1a7 | End transaction monitoring after handoff; ordinary Panda safety rejection is not ownership loss. |
| 1aa/1ab | Distinguish an empty historical temporary status from a current fault bitmap. |
| 1ac versus 16e | Preserve the verified radar-alive heartbeat body so reconstruction retains live radar/object inputs. |

## Standards and verification

The implementation follows relevant ISO 26262/21448 engineering principles: explicit safety goals, independent
authorization gates, bounded timing, unambiguous ownership, fail-closed diagnostics, configuration control, truthful
driver indication, and requirement-based regression tests. Formal certification would additionally require independent
hazard analysis, ASIL allocation, traceability, tool qualification decisions, fault injection, environmental testing,
and vehicle-level review.

Required regression evidence is:

- standard Panda plus both EV9 firmware images compile, link, and sign;
- all libpanda preinit state, timing, handoff, restore, watchdog, and USB-mutation tests pass;
- the complete Hyundai CAN-FD safety suite passes for the generic CCNC angle-long specialization;
- host startup/warm-rearm, dash/BSM/object, and route-replay tests pass;
- the feature-disabled build preserves generic Panda initialization and USB/CAN semantics;
- automatic vehicle logs cover every supported start path, motion-before-handoff, OFF restoration, host failure,
  unexpected stock reappearance, overrides, gear edges, and CAN fault/overflow cases.

Do not run a second direct `Panda()` client during READY or preinit. Its setup can reinitialize CAN and alter the system
being measured; use the receive-only Cereal capture or the automatic route log.
