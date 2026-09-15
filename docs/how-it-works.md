# How the mug talks: firmware, Bluetooth, and what the app actually does

Everything below was read live from an Ember Mug 2 (14 oz) on 2026-09-15 with `ember_mug_app.py --dump-gatt`, cross-checked against the two public reverse-engineering efforts: [orlopau/ember-mug](https://github.com/orlopau/ember-mug) (decompiled the Android app) and [sopelj/python-ember-mug](https://github.com/sopelj/python-ember-mug) (the library this app is built on).

## The hardware, from the outside

The mug exposes two Bluetooth LE services:

| Service | What it is |
|---|---|
| `fc543622-236c-4c94-8fa9-944a3e5353fa` | Ember's own service: 19 characteristics, everything the app uses. |
| `00001530-1212-efde-1523-785feabcd123` | **Nordic Semiconductor Legacy DFU** (Device Firmware Update). Characteristics 1531 (control point), 1532 (packet), 1534 (DFU version, reads `01 00` = v0.1). |

The DFU service tells you the mug runs on a Nordic nRF51/nRF52 radio SoC with Nordic's SoftDevice Bluetooth stack, and that firmware updates are Nordic's standard Legacy DFU dance: the phone app downloads a firmware bundle from Ember's servers, puts the mug into bootloader mode via the control point, streams the image over the packet characteristic, and the bootloader reboots into it. The Ember app is the only client that has the signed bundles; the DFU protocol itself is open (`nrfutil`, nRF Connect), but without an image there is nothing to flash, and Legacy DFU v0.1 does not verify signatures, so flashing anything home-made would brick or worse. In short: firmware updates stay the phone app's job.

Firmware characteristic `fc54000c` reads `96 01 0a 00`: firmware **406** (0x196), hardware **10**, no bootloader bytes (older mugs report 4 bytes; newer ones add a 2-byte bootloader version). orlopau documented 394 in 2022, so 406 is a handful of releases later.

## Identity: the mug has a fixed MAC, macOS hides it

`fc54000d` (Mug ID) reads `c1 b0 64 17 d3 6a` + `PSSY52502146`. The first six bytes are the mug's real Bluetooth MAC address, `C1:B0:64:17:D3:6A`, which is exactly what System Settings → Bluetooth shows. The rest is the serial number (python-ember-mug drops the leading `P`; the app shows `SSY52502146`).

macOS never exposes that MAC to apps. CoreBluetooth hands out a per-Mac random UUID instead (a value like `C9210D66-2F9B-…`), which is what `config.json` stores. That UUID is stable on this Mac but would be different on another Mac, which is why the app also falls back to scanning by name (`Ember Ceramic Mug`) if the saved address isn't seen.

## What each characteristic held at dump time

| UUID | Access | Raw | Meaning |
|---|---|---|---|
| `fc540001` | R/W | `45 4d 42 45 52` | Name: `EMBER` (≤16 chars) |
| `fc540002` | R | `86 18` | Current temp 0x1886 = 6278 → 62.78 °C (145.0 °F). Always °C ×100, little-endian, regardless of the unit setting. |
| `fc540003` | R/W | `85 18` | Target 62.77 °C. Writing `00 00` turns heating off. |
| `fc540004` | R/W | `01` | Display unit: 1 = °F, 0 = °C. Only affects what the phone app shows; the app now keeps this in sync with its own °F/°C choice. |
| `fc540005` | R | `1e` | Liquid level 30 of 30 → 100 %. |
| `fc540006` | R/W | `00 00 00 00 ff` | Date/time + tz offset. **Never set**: the phone app writes this; this app doesn't. It's only used for Ember's analytics. |
| `fc540007` | R | `06 01 f8 11 00` | Battery: **6 %**, on charger, battery temperature 0x11f8 = **46.00 °C**, legacy voltage byte 0. python-ember-mug only decodes the first two bytes; the app now reads the temperature too. |
| `fc540008` | R | `06` | Liquid state 6 = at target ("Perfect"). 0 standby, 1 empty, 2 filling, 3 cold/no control, 4 cooling, 5 heating, 7 warm/no control. |
| `fc54000a` | W | | Last location (lat/long from the phone). Analytics. |
| `fc54000c` | R | `96 01 0a 00` | Firmware 406, hardware 10. |
| `fc54000d` | R | MAC + serial | See above. |
| `fc54000e` | R | 20 bytes | **DSK** — device secret key, factory-set, read-only. |
| `fc54000f` | R/W | 20 bytes | **UDSK** — user device secret key. Written by the Ember app during first setup. Non-zero here, which is what makes the mug accept writes from anyone. |
| `fc540010` | R/W | `00` | Control register address (used for firmware-level settings, e.g. the temperature lock). |
| `fc540011` | R/W | (empty) | Control register data. Has a Report Reference descriptor with 20 bytes of opaque data. |
| `fc540012` | Notify | | **Push events**: 1-byte codes (1 battery, 2 charger on, 3 charger off, 4 target changed, 5 drink temp changed, 6 auth missing, 7 level changed, 8 state changed). The mug doesn't push values, just "go re-read X". |
| `fc540013` | Notify | | Statistics stream: the mug's on-device drink log, drained to the first subscriber. Decoded in [statistics-stream.md](statistics-stream.md). |
| `fc540014` | R/W | `ff 00 80 ff` | LED RGBA: 255, 0, 128, 255 → pink. |

## Authentication: there isn't much

There is no challenge/response. The mug has two 20-byte keys, DSK (factory, read-only) and UDSK (writable). A factory-fresh or factory-reset mug has an all-zero UDSK and **refuses writes** until the official app has set it; after that, any connected central can read and write everything. The Ember app never re-checks the key on connect. Push event 6 ("auth info not found") is what the mug sends when the UDSK is zero.

So "pairing" with the mug is really two separate things:

1. **BLE bonding with the Mac.** Done once in System Settings while the mug's LED pulses blue. macOS stores the link keys; the mug stores the Mac in its bond table. python-ember-mug tries `client.pair()` after connecting, which bleak's CoreBluetooth backend can't do explicitly (hence the harmless `Pairing not implemented` warning in the log); macOS pairs implicitly when an encrypted characteristic is first touched.
2. **UDSK provisioning.** Done once by the Ember phone app. Survives forever unless you factory-reset the mug (15-second hold), after which the phone app has to touch it again before this app can write.

## Why only one device at a time

The Nordic SoftDevice the mug runs is configured as a peripheral with a single connection slot. It advertises when awake and unconnected; the moment one central connects, advertising stops (or continues as non-connectable), so a second central can't even find it, let alone connect. That is what you see in the app as *found but refused* (advertising but connect times out; usually the phone got there first) versus *not found* (mug asleep, out of range, or already connected and not advertising). This is a firmware design choice; nothing on the Mac side can override it. Bonding is many-to-one, connecting is one-at-a-time.

The mug sleeps (stops advertising, drops everything) after a while when it's empty and off the coaster, and wakes on the coaster, when liquid is added, or when the button is pressed. Low battery makes it refuse connections outright, which is what happened at the start of today's session (solid red LED).

## The battery, now that we can see its temperature

46 °C is the interesting number. Lithium cells are typically not charged above ~45 °C, and the mug's battery sits directly under a heating element that was holding 63 °C liquid. While the mug is both heating and on the coaster, the coaster's power goes to the heater and the charger backs off to protect the cell. That fits what the app logged: 6 % on the coaster for over an hour with zero gain while the mug held a hot drink. The Ember app's own advice ("charge empty") is the same thing said politely. Expect the percentage to move only once the drink is gone or heating is off. The app now shows battery temperature under *Mug Info* and in `--status` as `battery_temp_c`, so a stubborn 6 % next to a 40-something battery temperature reads as "too hot to charge" rather than "battery is dead".

## How the app connects, step by step

1. `find_device(mac=<saved UUID>)` runs a CoreBluetooth scan for up to 15 s and returns the first advertisement from that address; if none, it scans again for anything named `Ember…`.
2. `EmberMug.connection()` calls `bleak_retry_connector.establish_connection`, which retries the GATT connect a few times with timeouts (that's the ~80 s "Failed to connect after 4 attempts" you see when the phone holds the mug), then subscribes to the push-event characteristic.
3. `update_initial()` reads the once-only stuff (ID, DSK/UDSK, firmware, time); `update_all()` reads the live values.
4. From then on the loop wakes once a second: if any push events arrived, it re-reads just the characteristics they named; every 60 s it re-reads everything as a safety net and re-applies the saved LED colour and unit if something else changed them.
5. If the client drops, or the app wants to hand the mug to the phone, the loop exits, disconnects cleanly (which lets the mug advertise again immediately), and goes back to step 1 with backoff.

`--dump-gatt` re-runs the full service walk on demand and writes `~/Library/Application Support/EmberMug/gatt.json`.
