# The statistics stream (`fc540013`), decoded

Status: first public decode, 2026-09-15, from one Ember Mug 2 (firmware 406). Everything marked **confirmed** was matched second-for-second against the app's own log; everything marked *hypothesis* fits the bytes but has only one or two samples behind it. The raw capture lives in `~/Library/Application Support/EmberMug/stats-capture.jsonl` (`ember_mug_app.py --stats` prints it).

## What it is

The mug keeps a **drink log on the device**. It is not a live telemetry feed: the mug buffers records as things happen and **drains the whole backlog to the first client that subscribes** to `fc540013`. Subscribing again afterwards yields only a `05` end-marker; the backlog is gone. New records do arrive live on an open subscription, delayed by ~15–20 s.

This is almost certainly the raw feed behind the Ember app's analytics (the app subscribes on connect, drains the log, and uploads it to `collector.embertech.com`). It means whoever connects first gets the log, so with this app running, Ember's app sees an empty log next time it connects.

Today's first subscribe produced 48 packets in five seconds: a short earlier session plus the full log of the current drink, poured at 11:07:33.

## Framing

Every notification is one of four packet types:

| First byte | Meaning |
|---|---|
| `01 00 LL …` | A complete record; `LL` = payload length (8 or 10 seen). |
| `02 00 10 …` | First fragment of a longer record; always 16 payload bytes. |
| `04 01 NN …` | Final fragment of the preceding `02` record; `NN` = this fragment's length (2, 3 or 8 seen). |
| `05` | End of batch. |

The 16-byte split is the mug working within a 20-byte ATT MTU. Reassemble `02`+`04` before parsing.

## Record layout

Reassembled payloads all start the same way:

```
00 | kind | sub | tttttttt | ff | fields…
```

- `kind`: record type (below).
- `sub`: for kind `05` it is the new liquid state; for the other kinds it is constant per kind (looks like a field-layout id).
- `t`: **uint32 big-endian time**. What it counts depends on whether the mug has been given a clock:
  - **No clock set** (`fc540006` reads `00 00 00 00 ff`, i.e. the phone app has never touched it, or the mug was reset): seconds since the drink was poured. Confirmed: the counter reset to 3 at the fill event, and the live record for a target write at 13:12:03 read 7474 → t₀ = 11:07:29.
  - **Clock set**: absolute Unix time. Writing `fc540006` (uint32 little-endian Unix time + int8 UTC offset in hours, the format the phone app uses) makes every subsequent record carry a real timestamp; confirmed 2026-09-15 17:35, when two target writes at 17:36:26 and 17:36:52 produced records stamped `6a a9 c8 74` / `6a a9 c8 8e` (1789511796 / 1789511822), about 10 s after the writes, the same lag the relative records showed. Reading `fc540006` back only returns what was written (it does not tick), but the mug clearly keeps time internally from that point. The app now sets the clock on every connect, so backlog drinks poured while the Mac was away carry exact pour times instead of being anchored from packet arrival. `drinklog.py` handles both: a `t` above 10⁹ is absolute (`t_abs`), and the drink's relative `t` is derived from its first record.
- All multi-byte numbers are **big-endian** (the GATT characteristics themselves are little-endian, so this log is written by a different code path). Temperatures are °C × 100. `7f ff` / `7f ff ff ff` are "no value" sentinels (INT16_MAX / INT32_MAX); trailing `ff` bytes are padding.

## Record kinds

| kind | sub | length | fields | meaning |
|---|---|---|---|---|
| `05` | state | 8 | none | **Liquid state change** (confirmed for 2 = filling, 6 = reached target). Only seen at the start of a drink. |
| `07` | `05` | 10 | int16 target | **Target temperature written by an app.** Confirmed: five records match the app's five BLE writes today to the second (11:30:34, 11:36:04, 11:43:17, 11:56:19, 13:12:04). |
| `0f` | `03` | 16+2 | int16 target, then nulls | **Heater (re)engaged toward the target** — logged when the drink starts and each time the mug is set back down after being lifted (matches every Standby→Heating transition today, plus the 13 s mark of the pour). Restarts the 10-minute sample timer. |
| `10` | `04` | 16+3 | uint8 liquid level (0–30), int16 current temp, then nulls | **Periodic sample every 600 s**, timer reset by each `0f`. Today: level 30 → 6 → 5 as the coffee went down, temps 59.5–62.8 °C, all matching the app's readings within 0.3 °C. |
| `15` | `07` | 16+8 | `05`, `00`, uint16 A, int16 B, int16 B again, `ff ff ff ff`, `01 58`, `00 93` | **Snapshot at pour (and at first "reached target")**. *Hypothesis:* A = battery percent, B = battery temperature °C×100. Values seen: A = 38 then 42 five minutes later (+4 % while on the coaster), 41 today; B = 33.57, 24.68, 33.39 °C. The trailing `01 58 00 93` (344, 147) was identical in all three snapshots, so it is a constant (limits? firmware parameters?), not a measurement. |

## Today's drink, as the mug logged it

| t (s) | wall clock | record | app log |
|---|---|---|---|
| 3 | 11:07:32 | filling; snapshot (41 %, 33.4 °C) | (app not yet connected) |
| 13 | 11:07:42 | heater engaged, target 62.77 | |
| 613 / 1213 / 1813 | 11:17 / 11:27 / 11:37 | samples: level 30, 62.5 / 62.5 / 59.5 °C | 62.78 / 62.78 / ~59.5 |
| 1384 | 11:30:34 | target set 57.21 (135 °F) | schedule wrote 57.22 at 11:30:35 |
| 1714 | 11:36:04 | target set 62.76 | `--set-temp 145` at 11:36:05 |
| 2136 | 11:43:06 | heater engaged 62.76 | Standby→Heating 11:43:17 |
| 2147 | 11:43:17 | target set 57.21 | schedule wrote 57.22 at 11:43:18 |
| 2510, 2633 | 11:49:20, 11:51:23 | heater engaged 57.21 ×2 | Standby→Heating 11:49:32; Perfect blip 11:51:38 |
| 2929 | 11:56:19 | target set 62.76 | `--set-temp 145` at 11:56:19 |
| 3233 / 3833 | 12:01 / 12:11 | samples: level 30, 62.5 °C | Perfect, 62.78 |
| 4277 / 4385 / 4636 | 12:18:47 / 12:20:35 / 12:24:46 | heater engaged 62.76 ×3 | Standby→Heating 12:18:59, 12:20:47, 12:24:58 |
| 5236 / 5836 / 6436 / 7036 | 12:34 → 13:04 | samples: level 6, 5, 5, 5; 62.8 / 62.8 / 61.5 / 62.6 °C | level 6 from 12:26, 5 from 12:41 |
| 7474 | 13:12:04 | target set 60.00 (140 °F) — arrived live 18 s later | `--set-temp 140` at 13:12:03 |

Every heater-engaged record lands ~12 s before the app's log line for the same event, which is the app's notification latency, not the mug's.

## Control register (`fc540010` / `fc540011`)

The register pair works as address-select then read: write a byte to `fc540010`, read `fc540011`. Scanned 2026-09-15 (addresses 0–24, 32, 64, 100, 128, 200, 254, 255; only the address side was written, and it was restored to 0 afterwards):

| address | data | meaning |
|---|---|---|
| `00` | (empty) | Nothing. This is what python-ember-mug's `get_battery_voltage()` reads, so that attribute is 0 bytes on this firmware. |
| `01` | `6a d3 17 64 b0 c1` | The mug's Bluetooth MAC, little-endian (the same bytes lead `fc54000d`). |
| `02` | `98 3e 00 20 34 25 00 20 a5 2f 00 00 34 06 00 20 7f 20 00 00` | Five 32-bit LE words: `0x20003e98`, `0x20002534`, `0x00002fa5`, `0x20000634`, `0x0000207f`. Three are nRF SRAM addresses (SRAM starts at `0x20000000`), so this is a firmware debug view (stack/heap pointers and two counters), not a setting. |
| `03` | `54 4a 4f 20 3b 2d 29` | ASCII **`TJO ;-)`**. A firmware engineer's initials. Hello, TJO. |
| `04` and up | `78 56 34 12` | `0x12345678`: the "not implemented" placeholder, identical at every address tried through `ff`. |

`fc540011` also carries a 20-byte `0x2908` descriptor reading `92 00 ff d6 12 00 7f a8 00 00 fe ec c0 89 f3 ee df f7 80 00`, which looks like uninitialised memory. So on firmware 406 the control register is a debug port with a joke in it, not where the temperature lock or a log acknowledgement lives; whatever the phone app does there goes through writes to the data side, which we have not tried.

## Open questions

- Whether the `15` snapshot's A/B really are battery percent and temperature. If so, the mug's own log said **41 %** at 11:07 while the battery characteristic has read a flat **6 %** since 11:13 — which would point at a confused fuel gauge (46 °C cell) rather than an empty battery. A second drink's snapshot will settle it.
- Why the `05` state records only appear at the start of a drink.
- What `01 58 00 93` is.
- Whether the Ember app acknowledges/clears the log explicitly or the mug simply forgets what it has sent. The control register's readable side is a debug view (above); an acknowledgement would have to be a write to its data side, which we have not tried.
- Whether the mug's clock survives sleep and for how long it drifts; and whether the ~10 s stamp lag is the mug's clock running ahead or the record being written when the event is processed.

## Capture setup

The app subscribes on every connection (`_subscribe_statistics`) and appends each packet with a snapshot of the mug's state. `--stats-resync` drops and re-adds the subscription to test flush behaviour; `--stats` prints the table.
