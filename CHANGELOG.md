# Changelog

## Unreleased

### Fixed
- A pour the app saw was logged as two drinks (an empty one plus the real one) once the mug's own "filling" record arrived, and the real drink lost its exact pour time.
- Closing the setup window with its close button left the app stuck in "Setup in progress…" until relaunch.
- A malformed or unreadable CLI command could stop the Bluetooth thread for good; a bad `config.json` was silently replaced with defaults on the next save (it is now kept as `config.json.bad`).
- Several CLI flags in one call (`--set-temp 140 --led red`) no longer overwrite each other; commands the app can't act on within 60 s are dropped instead of running on the next connect.
- `--set-temp` rejects values outside the mug's range (it used to turn heating off for `0`/`32F` and set maximum heat for `nan`); `--handoff` takes 1–1440 minutes.
- The drink tracker and the history deques are locked, since the Bluetooth thread writes them while the menu reads them.

### Changed
- `config.json`, `drinks.jsonl`, `current-drink.json` and `daily.json` are written atomically; a torn line in `drinks.jsonl` or `history.jsonl` is skipped.
- `history.jsonl` is compacted to the last 30 days on launch and read incrementally by the drink log (it was re-read in full on every packet and menu refresh).
- Statistics packets are logged at DEBUG, and log lines are no longer duplicated into the unrotated launcher log.

### Added
- Unit tests for the drink-log decoder and the app's GUI-free helpers: `python -m unittest discover -s tests`.

## 2.0.0 — 2026-09-15

A rewrite of the app around push updates, with onboarding, an instrument-panel popover, and the mug's own drink log.

### Added
- **Setup window** on first launch and under *Set Up Mug…*: pairing steps, a live scan listing every Ember device in range with signal strength, °F/°C choice, optional rename.
- **Instrument panel popover** on left-click: gauges for drink temperature (with the target tick), level and battery; state and ready-in estimate; today's totals; one-tap presets with the current one marked; the current drink's temperature trace; recent cups. Right-click (or Control-click) opens the settings menu.
- **Drink log** (`drinklog.py`): decodes the mug's on-device statistics stream (`fc540013`, documented in `docs/statistics-stream.md`) and merges it with the app's own readings into `drinks.jsonl`, `daily.json` and a full-page report. Cups poured while the Mac was away are recorded on the next connect.
- **Hand off to phone**: release the mug for N minutes so the Ember app can take it, then reconnect automatically (`--handoff`, `--takeback`).
- **Ready-in estimate** while heating, a temperature-history window, and readings logged to `history.jsonl`.
- **Time-of-day schedule** (opt-in) applied when the mug is refilled from empty.
- **Mug Info** (name, model, serial, firmware, address) and rename from the menu.
- **CLI and Raycast**: `--status [--brief]`, `--set-temp`, `--heating-off`, `--led`, `--refresh`, `--setup`, `--panel`, `--handoff`, `--takeback`, `--version`; `raycast/ember-mug.sh` script command.
- **Battery temperature** under *Mug Info*, read from the battery characteristic (charging pauses while the pack is hot, which is why a full mug charges slowly).
- Docs: `docs/how-it-works.md` (GATT layout, firmware, connection model) and `docs/statistics-stream.md` (the decoded log format).

### Changed
- Built on `python-ember-mug`: the mug pushes changes instead of the app polling; a full refresh runs once a minute as a safety net.
- Reconnects on its own with backoff when the mug sleeps, and tells "connected to another device" apart from "not reachable".
- Menu bar icon is the system cup symbol (SF Symbol) instead of an emoji; configurable via `menu_icon`.
- LED colour chosen here is remembered and re-applied if the phone app changes it.
- `build_app.sh` launches Python detached instead of `exec`ing it (on recent macOS the status item otherwise never draws) and hides the Dock icon by activation policy.

### Fixed
- Schedule no longer lowers the target on connect or when the mug goes to standby; it only applies on a refill from empty.
- Popover content sized to the screen, labels and ticks readable (≥ 6:1 contrast), real buttons with focus rings, status shown in words as well as colour.

## 1.0.0

Initial release: menu bar temperature and battery, presets, LED colour, notifications.
