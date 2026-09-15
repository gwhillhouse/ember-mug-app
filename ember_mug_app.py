#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "rumps>=0.4.0",
#     "python-ember-mug>=1.2.1",
#     "pyobjc-framework-WebKit>=10",
# ]
# ///

"""
Ember Mug Menu Bar App - macOS control for Ember smart mugs.

Built on python-ember-mug, so the mug pushes changes to us (temperature,
battery, liquid state) instead of us hammering it with reads every few seconds.

Run the menu bar app:      python ember_mug_app.py
Read status from scripts:  python ember_mug_app.py --status
Control from scripts:      python ember_mug_app.py --set-temp 140 | --led red | --heating-off

https://github.com/gwhillhouse/ember-mug-app
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import plistlib
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, time as dtime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional

APP_NAME = "Ember Mug"
__version__ = "2.0.0"
BUNDLE_ID = "com.gwhillhouse.ember-mug"
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
LOG_DIR = Path.home() / "Library" / "Logs" / "EmberMug"
LOG_PATH = LOG_DIR / "ember-mug.log"
SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "EmberMug"
STATUS_PATH = SUPPORT_DIR / "status.json"
COMMAND_PATH = SUPPORT_DIR / "command.json"
HISTORY_PATH = SUPPORT_DIR / "history.jsonl"
STATS_CAPTURE_PATH = SUPPORT_DIR / "stats-capture.jsonl"  # raw fc540013 notifications, for reverse engineering
LAUNCH_AGENT_PATH = Path.home() / "Library" / "LaunchAgents" / f"{BUNDLE_ID}.plist"

# Ember's supported heating range (the mug clamps anything outside this).
MIN_TEMP_C, MAX_TEMP_C = 49.0, 63.0
MIN_TEMP_F, MAX_TEMP_F = 120, 145

DEFAULT_PRESETS_F = {"Cold": 120, "Warm": 130, "Hot": 140, "Very Hot": 145}
LEGACY_PRESET_KEYS = {"cold": "Cold", "warm": "Warm", "hot": "Hot", "very_hot": "Very Hot", "max": "Max"}

LED_COLOURS = [
    ("White (Default)", (255, 255, 255)),
    ("Red", (255, 0, 0)),
    ("Orange", (255, 100, 0)),
    ("Yellow", (255, 200, 0)),
    ("Green", (0, 255, 0)),
    ("Cyan", (0, 255, 255)),
    ("Blue", (0, 0, 255)),
    ("Purple", (128, 0, 255)),
    ("Pink", (255, 0, 128)),
]
LED_BY_NAME = {label.split(" ")[0].lower(): rgb for label, rgb in LED_COLOURS}

DEFAULT_CONFIG: dict[str, Any] = {
    "mug_address": "",
    "temperature_unit": "F",
    "menu_icon": "",  # "" = SF Symbol cup icon; set to text/emoji (e.g. "🫖") to use that instead
    "presets_f": DEFAULT_PRESETS_F,
    "full_refresh_seconds": 60,
    "scan_timeout_seconds": 15,
    "notify_on_perfect": True,
    "notify_low_battery": True,
    "low_battery_percent": 20,
    "led_colour": None,
    "sync_unit_to_mug": True,
    "auto_schedule": False,
    # Time-of-day targets, applied when the mug is refilled (empty -> filling/heating) if auto_schedule is on.
    "schedule": [
        {"label": "Morning", "start": "05:00", "end": "11:00", "temp_f": 140},
        {"label": "Afternoon", "start": "11:00", "end": "17:00", "temp_f": 135},
        {"label": "Evening", "start": "17:00", "end": "23:00", "temp_f": 130},
    ],
    "history_hours": 12,
}

log = logging.getLogger("ember-mug")


# --------------------------------------------------------------------------- #
# Config / logging / small helpers (no GUI imports here so --status stays light)
# --------------------------------------------------------------------------- #


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    handler = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3)
    handler.setFormatter(fmt)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(stream)
    logging.getLogger("bleak_retry_connector").setLevel(logging.WARNING)


def load_config() -> dict[str, Any]:
    config = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                user = json.load(f)
        except json.JSONDecodeError as e:
            log.error("config.json is invalid JSON (%s); using defaults", e)
            user = {}
        config.update({k: v for k, v in user.items() if k in DEFAULT_CONFIG})
        if "default_temp_presets" in user and "presets_f" not in user:  # original config format
            config["presets_f"] = {LEGACY_PRESET_KEYS.get(k, k.title()): v for k, v in user["default_temp_presets"].items()}

    if config["mug_address"] in ("YOUR-MUG-MAC-ADDRESS-HERE", None):
        config["mug_address"] = ""

    presets: dict[str, int] = {}
    for label, value in (config.get("presets_f") or {}).items():
        try:
            presets[str(label)] = max(MIN_TEMP_F, min(MAX_TEMP_F, int(value)))
        except (TypeError, ValueError):
            log.warning("Preset %r is not a number; skipping", label)
    config["presets_f"] = presets or dict(DEFAULT_PRESETS_F)

    if config["temperature_unit"] not in ("F", "C"):
        config["temperature_unit"] = "F"
    if not isinstance(config.get("schedule"), list):
        config["schedule"] = []
    return config


def save_config(config: dict[str, Any]) -> None:
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(config, f, indent=2)
            f.write("\n")
    except OSError as e:
        log.warning("Could not save config.json: %s", e)


def c_to_f(temp_c: float) -> float:
    return temp_c * 9 / 5 + 32


def f_to_c(temp_f: float) -> float:
    return (temp_f - 32) * 5 / 9


def notify(title: str, message: str) -> None:
    """Post a macOS notification without needing a signed bundle."""
    script = 'display notification "{}" with title "{}"'.format(message.replace('"', '\\"'), title.replace('"', '\\"'))
    try:
        subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        log.warning("Notification failed: %s", e)


def parse_hhmm(value: str) -> Optional[dtime]:
    try:
        hh, mm = value.split(":")
        return dtime(int(hh), int(mm))
    except (ValueError, AttributeError):
        return None


def schedule_rule_for(config: dict[str, Any], now: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    """Return the schedule rule covering `now`, if any (rules may wrap past midnight)."""
    now = now or datetime.now()
    t = now.time()
    for rule in config.get("schedule", []):
        start, end = parse_hhmm(rule.get("start", "")), parse_hhmm(rule.get("end", ""))
        if not start or not end or "temp_f" not in rule:
            continue
        inside = start <= t < end if start <= end else (t >= start or t < end)
        if inside:
            return rule
    return None


def launcher_command() -> list[str]:
    launcher = os.environ.get("EMBER_MUG_LAUNCHER")
    if launcher and Path(launcher).exists():
        return [launcher]
    return [sys.executable, str(Path(__file__).resolve())]


def login_item_installed() -> bool:
    return LAUNCH_AGENT_PATH.exists()


def set_login_item(enabled: bool) -> None:
    uid = os.getuid()
    if enabled:
        LAUNCH_AGENT_PATH.parent.mkdir(parents=True, exist_ok=True)
        plist = {
            "Label": BUNDLE_ID,
            "ProgramArguments": launcher_command(),
            "RunAtLoad": True,
            "KeepAlive": False,
            "AbandonProcessGroup": True,
            "ProcessType": "Interactive",
            "StandardOutPath": str(LOG_DIR / "launchagent.log"),
            "StandardErrorPath": str(LOG_DIR / "launchagent.log"),
        }
        with open(LAUNCH_AGENT_PATH, "wb") as f:
            plistlib.dump(plist, f)
        subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(LAUNCH_AGENT_PATH)], check=False)
        log.info("Installed login item %s", LAUNCH_AGENT_PATH)
    else:
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{BUNDLE_ID}"], check=False)
        try:
            LAUNCH_AGENT_PATH.unlink()
        except FileNotFoundError:
            pass
        log.info("Removed login item")


def read_status_file() -> Optional[dict[str, Any]]:
    try:
        with open(STATUS_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def write_command(command: dict[str, Any]) -> None:
    SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = COMMAND_PATH.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(command, f)
    tmp.replace(COMMAND_PATH)


# --------------------------------------------------------------------------- #
# Temperature history
# --------------------------------------------------------------------------- #


class TemperatureHistory:
    """Rolling in-memory samples plus an append-only JSONL log on disk."""

    def __init__(self, hours: float) -> None:
        self.window = hours * 3600
        self.samples: deque[tuple[float, float, Optional[float], str]] = deque()  # (ts, current_c, target_c, state)
        self.battery: deque[tuple[float, float, bool]] = deque()  # (ts, percent, on_base)
        self._last_logged: Optional[tuple[float, Optional[float], str]] = None
        self._load()

    def _load(self) -> None:
        cutoff = time.time() - self.window
        try:
            with open(HISTORY_PATH) as f:
                for line in f:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if row.get("ts", 0) >= cutoff:
                        self.samples.append((row["ts"], row["current_c"], row.get("target_c"), row.get("state", "")))
                        if row.get("battery") is not None:
                            self.battery.append((row["ts"], row["battery"], bool(row.get("on_base"))))
        except OSError:
            pass

    def add(self, current_c: float, target_c: Optional[float], state: str, battery: Optional[float] = None, on_base: bool = False, level: Optional[int] = None) -> None:
        now = time.time()
        key = (round(current_c, 1), round(target_c, 1) if target_c else None, state, battery, on_base, level)
        # Log at most once a minute unless something changed.
        if self._last_logged == key and self.samples and now - self.samples[-1][0] < 60:
            return
        self._last_logged = key
        self.samples.append((now, current_c, target_c, state))
        if battery is not None:
            self.battery.append((now, battery, on_base))
        cutoff = now - self.window
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()
        while self.battery and self.battery[0][0] < cutoff:
            self.battery.popleft()
        try:
            SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
            with open(HISTORY_PATH, "a") as f:
                f.write(json.dumps({"ts": now, "current_c": current_c, "target_c": target_c, "state": state, "battery": battery, "on_base": on_base, "level": level}) + "\n")
        except OSError:
            pass

    def battery_trend(self) -> Optional[dict[str, Any]]:
        """Charge/drain rate (%/hour) over the current uninterrupted on-base or off-base stretch."""
        if len(self.battery) < 2:
            return None
        now = time.time()
        latest_ts, latest_pct, on_base = self.battery[-1]
        # Walk back from the newest sample until the on-base state flips.
        contiguous: list[tuple[float, float]] = []
        for ts, pct, base in reversed(self.battery):
            if base != on_base:
                break
            contiguous.append((ts, pct))
        if len(contiguous) < 2:
            return {"on_base": on_base, "minutes": 0.0, "rate_per_hour": None, "delta": 0.0}
        first_ts, first_pct = contiguous[-1]
        minutes = (now - first_ts) / 60
        delta = latest_pct - first_pct
        rate = delta / (minutes / 60) if minutes >= 5 else None
        return {"on_base": on_base, "minutes": minutes, "rate_per_hour": rate, "delta": delta}

    def eta_seconds(self, current_c: float, target_c: float) -> Optional[float]:
        """Estimate seconds until target using the heating rate over the last few minutes."""
        now = time.time()
        recent = [(ts, temp) for ts, temp, _, _ in self.samples if now - ts <= 240]
        if len(recent) < 3 or recent[-1][0] - recent[0][0] < 30:
            return None
        n = len(recent)
        mean_t = sum(ts for ts, _ in recent) / n
        mean_v = sum(v for _, v in recent) / n
        var = sum((ts - mean_t) ** 2 for ts, _ in recent)
        if var == 0:
            return None
        slope = sum((ts - mean_t) * (v - mean_v) for ts, v in recent) / var  # °C per second
        remaining = target_c - current_c
        if remaining <= 0 or slope <= 0.002:
            return None
        return remaining / slope


# --------------------------------------------------------------------------- #
# Menu bar app
# --------------------------------------------------------------------------- #


def run_app() -> None:
    import rumps
    from bleak import BleakError, BleakScanner
    from ember_mug.consts import EMBER_BLE_SIG, LiquidState, MugCharacteristic, TemperatureUnit
    from ember_mug.data import Colour, MugData
    from ember_mug.mug import EmberMug
    from ember_mug.scanner import find_device
    from ember_mug.utils import get_model_info_from_advertiser_data
    from AppKit import (
        NSApplication,
        NSApplicationActivationPolicyAccessory,
        NSBackingStoreBuffered,
        NSBezierPath,
        NSBox,
        NSButton,
        NSColor,
        NSFont,
        NSFontAttributeName,
        NSForegroundColorAttributeName,
        NSImage,
        NSImageSymbolConfiguration,
        NSImageView,
        NSMakeRect,
        NSPopUpButton,
        NSProgressIndicator,
        NSSegmentedControl,
        NSTextField,
        NSView,
        NSWindow,
        NSWorkspace,
    )
    from Foundation import NSObject, NSString, NSURL
    from PyObjCTools import AppHelper
    import objc

    sys.path.insert(0, str(SCRIPT_DIR))
    import drinklog  # noqa: PLC0415

    try:
        from AppKit import NSAppearance, NSPopover, NSScreen, NSViewController
        from WebKit import WKWebView, WKWebViewConfiguration

        HAVE_WEBKIT = True
    except Exception:  # noqa: BLE001  (pyobjc-framework-WebKit not installed: fall back to the plain menu)
        HAVE_WEBKIT = False

    WINDOW_STYLE = 1 | 2 | 4  # titled, closable, miniaturizable

    def make_label(text: str, x: float, y: float, w: float, h: float, bold: bool = False, size: float = 13) -> Any:
        label = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
        label.setStringValue_(text)
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setEditable_(False)
        label.setSelectable_(False)
        label.setFont_(NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size))
        return label

    def make_wrapping_label(text: str, x: float, y: float, w: float, h: float) -> Any:
        label = NSTextField.wrappingLabelWithString_(text)
        label.setFrame_(NSMakeRect(x, y, w, h))
        label.setSelectable_(False)
        return label

    def make_button(title: str, x: float, y: float, w: float, h: float, target: Any, action: str) -> Any:
        button = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
        button.setTitle_(title)
        button.setBezelStyle_(1)  # rounded
        button.setTarget_(target)
        button.setAction_(action)
        return button

    def show_window(window: Any) -> None:
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        window.makeKeyAndOrderFront_(None)

    # ----------------------------------------------------------- setup wizard --

    class StepBadge(NSView):
        """A filled accent-colour circle with a step number in it."""

        def initWithFrame_number_(self, frame, number):
            self = objc.super(StepBadge, self).initWithFrame_(frame)
            if self is None:
                return None
            self.number = str(number)
            self.done = False
            return self

        def drawRect_(self, rect):
            bounds = self.bounds()
            (NSColor.systemGreenColor() if self.done else NSColor.controlAccentColor()).set()
            NSBezierPath.bezierPathWithOvalInRect_(bounds).fill()
            text = "✓" if self.done else self.number
            attrs = {NSFontAttributeName: NSFont.boldSystemFontOfSize_(13), NSForegroundColorAttributeName: NSColor.whiteColor()}
            size = NSString.stringWithString_(text).sizeWithAttributes_(attrs)
            NSString.stringWithString_(text).drawAtPoint_withAttributes_(
                ((bounds.size.width - size.width) / 2, (bounds.size.height - size.height) / 2 - 0.5), attrs
            )

        @objc.python_method
        def set_done(self, done: bool) -> None:
            self.done = done
            self.setNeedsDisplay_(True)

    class SetupWindowController(NSObject):
        """One-window setup: pair instructions → scan → pick mug → units/name → save."""

        def initWithApp_(self, app):
            self = objc.super(SetupWindowController, self).init()
            if self is None:
                return None
            self.app = app
            self.found: list[tuple[Any, Any]] = []
            W, H = 560, 680
            self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, W, H), WINDOW_STYLE, NSBackingStoreBuffered, False
            )
            self.window.setTitle_("Set Up Ember Mug")
            self.window.setReleasedWhenClosed_(False)
            self.window.center()
            v = self.window.contentView()
            secondary = NSColor.secondaryLabelColor()

            # --- header: big cup icon, title, subtitle ---
            icon_view = NSImageView.alloc().initWithFrame_(NSMakeRect(24, H - 84, 56, 56))
            icon = NSImage.imageWithSystemSymbolName_accessibilityDescription_("cup.and.saucer.fill", APP_NAME)
            if icon is not None:
                config = NSImageSymbolConfiguration.configurationWithPointSize_weight_(46, 0)
                icon_view.setImage_(icon.imageWithSymbolConfiguration_(config))
                icon_view.setContentTintColor_(NSColor.controlAccentColor())
            v.addSubview_(icon_view)
            v.addSubview_(make_label("Set up your Ember mug", 96, H - 60, 440, 30, bold=True, size=22))
            subtitle = make_label("Takes about a minute. The mug stays paired with your phone too.", 96, H - 82, 440, 20)
            subtitle.setTextColor_(secondary)
            v.addSubview_(subtitle)

            # --- steps ---
            def add_step(number: int, top: float, title: str, body: str, body_height: float):
                badge = StepBadge.alloc().initWithFrame_number_(NSMakeRect(24, top - 28, 26, 26), number)
                v.addSubview_(badge)
                v.addSubview_(make_label(title, 62, top - 26, 470, 22, bold=True, size=14))
                text = make_wrapping_label(body, 62, top - 30 - body_height, 470, body_height)
                text.setTextColor_(secondary)
                v.addSubview_(text)
                return badge

            self.badges = [
                add_step(1, H - 110, "Turn off Bluetooth on your phone",
                         "The mug talks to one device at a time, and the Ember app will keep grabbing it while you set this up.", 34),
                add_step(2, H - 186, "Pair the mug with this Mac",
                         "Hold the button on the bottom of the mug for 5–7 seconds until the LED pulses blue. In Bluetooth Settings, "
                         "click Connect next to “Ember Ceramic Mug”, then tap the mug button once to leave pairing mode.", 54),
                add_step(3, H - 350, "Wake it up and scan",
                         "Set the mug on its coaster or pour something in, then scan. Every Ember device in range will be listed.", 34),
            ]
            v.addSubview_(make_button("Open Bluetooth Settings…", 62, H - 308, 200, 28, self, "openBluetooth:"))
            self.scan_button = make_button("Scan for Mugs", 62, H - 448, 140, 28, self, "scan:")
            v.addSubview_(self.scan_button)
            self.spinner = NSProgressIndicator.alloc().initWithFrame_(NSMakeRect(210, H - 444, 18, 18))
            self.spinner.setStyle_(1)  # spinning
            self.spinner.setControlSize_(1)  # small
            self.spinner.setDisplayedWhenStopped_(False)
            v.addSubview_(self.spinner)
            self.status_label = make_label("", 234, H - 446, 302, 20)
            self.status_label.setTextColor_(secondary)
            v.addSubview_(self.status_label)

            # --- "Your mug" group ---
            box = NSBox.alloc().initWithFrame_(NSMakeRect(24, 66, W - 48, 138))
            box.setTitle_("Your mug")
            box.setTitleFont_(NSFont.boldSystemFontOfSize_(12))
            v.addSubview_(box)
            c = box.contentView()
            cw = c.bounds().size.width

            c.addSubview_(make_label("Mug", 12, 74, 60, 20))
            self.device_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(76, 70, cw - 88, 28), False)
            self.device_popup.addItemWithTitle_("Scan to find your mug")
            self.device_popup.setEnabled_(False)
            c.addSubview_(self.device_popup)

            c.addSubview_(make_label("Units", 12, 36, 60, 20))
            self.unit_control = NSSegmentedControl.alloc().initWithFrame_(NSMakeRect(76, 33, 110, 24))
            self.unit_control.setSegmentCount_(2)
            self.unit_control.setLabel_forSegment_("°F", 0)
            self.unit_control.setLabel_forSegment_("°C", 1)
            self.unit_control.setWidth_forSegment_(50, 0)
            self.unit_control.setWidth_forSegment_(50, 1)
            self.unit_control.setSelectedSegment_(0 if app.use_f else 1)
            c.addSubview_(self.unit_control)

            c.addSubview_(make_label("Name", 214, 36, 50, 20))
            self.name_field = NSTextField.alloc().initWithFrame_(NSMakeRect(262, 33, cw - 274, 24))
            self.name_field.setPlaceholderString_("Optional")
            c.addSubview_(self.name_field)
            hint = make_label("Up to 16 characters. Shows in the Ember app and under Mug Info; blank keeps the current name.", 12, 8, cw - 24, 18, size=10)
            hint.setTextColor_(secondary)
            c.addSubview_(hint)

            # --- footer ---
            v.addSubview_(make_button("Cancel", W - 254, 20, 100, 32, self, "cancel:"))
            self.save_button = make_button("Save & Connect", W - 150, 20, 126, 32, self, "save:")
            self.save_button.setKeyEquivalent_("\r")
            self.save_button.setEnabled_(False)
            v.addSubview_(self.save_button)
            self.window.setInitialFirstResponder_(self.scan_button)
            return self

        @objc.python_method
        def show(self) -> None:
            self.app.enter_setup_mode()
            if self.app.config["mug_address"]:
                self.status_label.setStringValue_("A mug is already set up; scan again to change it.")
            show_window(self.window)

        def openBluetooth_(self, sender):
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_("x-apple.systempreferences:com.apple.BluetoothSettings"))

        def scan_(self, sender):
            self.scan_button.setEnabled_(False)
            self.save_button.setEnabled_(False)
            self.spinner.startAnimation_(None)
            self.status_label.setStringValue_("Listening for mugs for 10 seconds…")
            self.app.submit_setup_scan(self._scan_done)

        @objc.python_method
        def _scan_done(self, found: list[tuple[Any, Any]], error: Optional[str]) -> None:
            self.scan_button.setEnabled_(True)
            self.spinner.stopAnimation_(None)
            self.found = found
            self.device_popup.removeAllItems()
            if error:
                self.status_label.setStringValue_(f"Scan failed: {error}")
                self.device_popup.addItemWithTitle_("Scan failed")
                return
            if not found:
                self.status_label.setStringValue_("No mug seen. Is it awake, paired with this Mac, and is your phone’s Bluetooth off?")
                self.device_popup.addItemWithTitle_("Nothing found — try again")
                self.device_popup.setEnabled_(False)
                return
            for device, adv in found:
                model = get_model_info_from_advertiser_data(adv)
                rssi = getattr(adv, "rssi", None)
                bars = "▂▄▆" if rssi is None or rssi > -60 else "▂▄" if rssi > -75 else "▂"
                self.device_popup.addItemWithTitle_(f"{device.name or 'Ember device'} · {model.name}   {bars}")
            self.device_popup.setEnabled_(True)
            self.save_button.setEnabled_(True)
            for badge in self.badges:
                badge.set_done(True)
            self.status_label.setStringValue_(f"Found {len(found)} mug{'s' if len(found) != 1 else ''} — pick yours below.")

        def cancel_(self, sender):
            self.window.close()
            self.app.leave_setup_mode()

        def save_(self, sender):
            index = self.device_popup.indexOfSelectedItem()
            if not self.found or index < 0 or index >= len(self.found):
                return
            device, _ = self.found[index]
            self.app.config["mug_address"] = device.address
            self.app.set_unit("F" if self.unit_control.selectedSegment() == 0 else "C", render=False)
            name = str(self.name_field.stringValue()).strip()
            self.app.pending_name = name or None
            save_config(self.app.config)
            log.info("Setup saved: %s", device.address)
            self.window.close()
            self.app.leave_setup_mode()

    # ---------------------------------------------------------- history view --

    class HistoryView(NSView):
        def initWithFrame_app_(self, frame, app):
            self = objc.super(HistoryView, self).initWithFrame_(frame)
            if self is None:
                return None
            self.app = app
            return self

        def drawRect_(self, rect):
            NSColor.windowBackgroundColor().set()
            NSBezierPath.fillRect_(self.bounds())
            samples = list(self.app.history.samples)
            w, h = self.bounds().size.width, self.bounds().size.height
            left, right, top, bottom = 44, 12, 12, 28
            font = NSFont.systemFontOfSize_(10)
            attrs = {NSFontAttributeName: font, NSForegroundColorAttributeName: NSColor.secondaryLabelColor()}
            if len(samples) < 2:
                NSString.stringWithString_("No readings yet — history fills in as the mug reports temperatures.").drawAtPoint_withAttributes_((left, h / 2), attrs)
                return
            t0, t1 = samples[0][0], samples[-1][0]
            span = max(t1 - t0, 600)
            temps = [s[1] for s in samples] + [s[2] for s in samples if s[2]]
            lo = min(temps) - 2
            hi = max(temps) + 2

            def X(ts: float) -> float:
                return left + (ts - t0) / span * (w - left - right)

            def Y(temp_c: float) -> float:
                return bottom + (temp_c - lo) / (hi - lo) * (h - top - bottom)

            # gridlines + labels
            NSColor.separatorColor().set()
            for i in range(5):
                temp_c = lo + (hi - lo) * i / 4
                y = Y(temp_c)
                NSBezierPath.strokeLineFromPoint_toPoint_((left, y), (w - right, y))
                NSString.stringWithString_(self.app.fmt_temp(temp_c)).drawAtPoint_withAttributes_((2, y - 6), attrs)
            for i in range(4):
                ts = t0 + span * i / 3
                x = X(ts)
                NSString.stringWithString_(datetime.fromtimestamp(ts).strftime("%H:%M")).drawAtPoint_withAttributes_((x - 14, 4), attrs)

            # target (dashed)
            target = NSBezierPath.bezierPath()
            target.setLineWidth_(1)
            target.setLineDash_count_phase_([4, 3], 2, 0)
            NSColor.systemOrangeColor().set()
            started = False
            for ts, _, tgt, _ in samples:
                if not tgt:
                    started = False
                    continue
                (target.lineToPoint_ if started else target.moveToPoint_)((X(ts), Y(tgt)))
                started = True
            target.stroke()

            # current temperature
            line = NSBezierPath.bezierPath()
            line.setLineWidth_(2)
            NSColor.systemBlueColor().set()
            for i, (ts, temp, _, _) in enumerate(samples):
                (line.moveToPoint_ if i == 0 else line.lineToPoint_)((X(ts), Y(temp)))
            line.stroke()

    class PopoverController(NSObject):
        """Left-click on the menu bar icon: an instrument panel in a popover. Right-click: the menu."""

        def initWithApp_(self, app):
            self = objc.super(PopoverController, self).init()
            if self is None:
                return None
            self.app = app
            self.last_reload = 0.0
            self.restore_y = 0  # scroll offset to put back after a refresh, so a 10 s reload doesn't jump to the top
            self.webview = WKWebView.alloc().initWithFrame_configuration_(NSMakeRect(0, 0, 520, 740), WKWebViewConfiguration.alloc().init())
            self.webview.setNavigationDelegate_(self)
            try:
                self.webview.setValue_forKey_(False, "drawsBackground")
            except Exception:  # noqa: BLE001
                pass
            controller = NSViewController.alloc().init()
            controller.setView_(self.webview)
            self.popover = NSPopover.alloc().init()
            self.popover.setContentViewController_(controller)
            self.popover.setContentSize_((520, 740))
            self.popover.setBehavior_(1)  # transient: closes on an outside click
            self.popover.setAnimates_(True)
            try:
                self.popover.setAppearance_(NSAppearance.appearanceNamed_("NSAppearanceNameVibrantDark"))
            except Exception:  # noqa: BLE001
                pass
            return self

        def statusClicked_(self, sender):
            event = NSApplication.sharedApplication().currentEvent()
            etype = event.type() if event is not None else 0
            control = bool(event is not None and event.modifierFlags() & (1 << 18))
            if etype in (3, 4) or control:  # right mouse down/up, or ctrl-click
                self.app.show_menu()
            else:
                self.toggle()

        @objc.python_method
        def toggle(self) -> None:
            if self.popover.isShown():
                self.popover.performClose_(None)
                return
            self.reload()
            button = self.app._nsapp.nsstatusitem.button()
            self.popover.showRelativeToRect_ofView_preferredEdge_(button.bounds(), button, 1)  # below the icon
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

        @objc.python_method
        def reload(self) -> None:
            try:
                self.webview.loadHTMLString_baseURL_(self.app.panel_html(), None)
                self.last_reload = time.time()
            except Exception:  # noqa: BLE001
                log.exception("popover render failed")

        @objc.python_method
        def maybe_refresh(self) -> None:
            if self.popover.isShown() and time.time() - self.last_reload > 10:
                self.last_reload = time.time()  # don't re-enter while the scroll query is in flight

                def then(result, error):
                    try:
                        self.restore_y = int(float(result or 0))
                    except (TypeError, ValueError):
                        self.restore_y = 0
                    self.reload()

                self.webview.evaluateJavaScript_completionHandler_("window.scrollY", then)

        def webView_didFinishNavigation_(self, webview, navigation):
            """Fit the popover to the rendered page (capped to the screen) and restore the scroll offset."""
            y, self.restore_y = self.restore_y, 0

            def sized(result, error):
                try:
                    h = int(float(result or 0))
                except (TypeError, ValueError):
                    h = 0
                if h > 0:
                    screen = NSScreen.mainScreen()
                    cap = int(screen.visibleFrame().size.height) - 24 if screen is not None else 900
                    new_h = max(420, min(h + 2, cap))
                    if abs(new_h - self.popover.contentSize().height) > 2:
                        self.popover.setContentSize_((520, new_h))
                if y:
                    webview.evaluateJavaScript_completionHandler_(f"window.scrollTo(0, {y})", None)

            webview.evaluateJavaScript_completionHandler_("document.documentElement.scrollHeight", sized)

        def webView_decidePolicyForNavigationAction_decisionHandler_(self, webview, action, handler):
            try:
                url = str(action.request().URL().absoluteString())
            except Exception:  # noqa: BLE001
                url = ""
            if url.startswith("embermug://"):
                handler(0)  # cancel the navigation; we handle it
                self.app.handle_action_url(url)
            else:
                handler(1)

    class EmberMenuBar(rumps.App):
        def __init__(self) -> None:
            self.config = load_config()
            self.icon_text = self.config["menu_icon"].strip()
            self.prefix = f"{self.icon_text} " if self.icon_text else "Ember "
            super().__init__(APP_NAME, title=f"{self.prefix}…", quit_button=None)

            self.loop: Optional[asyncio.AbstractEventLoop] = None
            self.mug: Optional[EmberMug] = None
            self.data: Optional[MugData] = None
            self.connected = False
            self.status_text = "Starting…"
            self.setup_mode = False
            self.paused_until: float = 0.0  # "hand off to phone" — don't reconnect until then
            self._seen_but_refused = False
            self.battery_temp_c: Optional[float] = None
            self.pending_name: Optional[str] = None
            self._reconnect_requested = asyncio.Event()
            self._stop = threading.Event()
            self._last_liquid_state: Optional[LiquidState] = None
            self._low_battery_notified = False
            self.history = TemperatureHistory(float(self.config["history_hours"]))
            self.tracker = drinklog.DrinkTracker()
            self.eta_seconds: Optional[float] = None
            self._setup_controller: Optional[SetupWindowController] = None
            self._popover: Optional[Any] = None
            self._history_window: Optional[Any] = None
            self._history_view: Optional[Any] = None

            # --- menu ---
            self.status_item = rumps.MenuItem("Status: Starting…")
            self.today_item = rumps.MenuItem("Today: —")
            self.battery_item = rumps.MenuItem("Battery: --")
            self.liquid_item = rumps.MenuItem("Liquid: --")
            self.current_item = rumps.MenuItem("Current: --")
            self.target_item = rumps.MenuItem("Target: --")
            self.eta_item = rumps.MenuItem("Ready in: --")
            self.led_item = rumps.MenuItem("LED: --")

            self.preset_items: dict[str, rumps.MenuItem] = {}
            temp_menu = []
            for label, temp_f in self.config["presets_f"].items():
                item = rumps.MenuItem(self._preset_title(label, temp_f), callback=self._make_preset_cb(temp_f))
                self.preset_items[label] = item
                temp_menu.append(item)
            temp_menu += [rumps.separator, rumps.MenuItem("Custom…", callback=self.custom_temperature), rumps.MenuItem("Heating Off", callback=self.heating_off)]

            self.led_items: dict[tuple[int, int, int], rumps.MenuItem] = {}
            led_menu = []
            for label, rgb in LED_COLOURS:
                item = rumps.MenuItem(label, callback=self._make_led_cb(rgb))
                self.led_items[rgb] = item
                led_menu.append(item)

            self.info_name_item = rumps.MenuItem("Name: --")
            self.info_model_item = rumps.MenuItem("Model: --")
            self.info_serial_item = rumps.MenuItem("Serial: --")
            self.info_firmware_item = rumps.MenuItem("Firmware: --")
            self.info_address_item = rumps.MenuItem("Address: --")
            self.info_unit_item = rumps.MenuItem("Mug display unit: --")
            self.info_battery_temp_item = rumps.MenuItem("Battery temperature: --")
            info_menu = [
                self.info_name_item, self.info_model_item, self.info_serial_item, self.info_firmware_item,
                self.info_address_item, self.info_unit_item, self.info_battery_temp_item, rumps.separator,
                rumps.MenuItem("Rename Mug…", callback=self.rename_mug),
            ]

            self.schedule_rule_items: list[rumps.MenuItem] = []
            schedule_menu = []
            for rule in self.config["schedule"]:
                item = rumps.MenuItem(self._rule_title(rule))
                self.schedule_rule_items.append(item)
                schedule_menu.append(item)
            self.auto_schedule_item = rumps.MenuItem("Apply automatically on refill", callback=self.toggle_auto_schedule)
            schedule_menu += [rumps.separator, self.auto_schedule_item, rumps.MenuItem("Apply now", callback=self.apply_schedule_now), rumps.MenuItem("Edit schedule (config.json)…", callback=self.open_config)]

            self.unit_f_item = rumps.MenuItem("Fahrenheit (°F)", callback=lambda _: self.set_unit("F"))
            self.unit_c_item = rumps.MenuItem("Celsius (°C)", callback=lambda _: self.set_unit("C"))
            self.notify_perfect_item = rumps.MenuItem("When drink reaches target", callback=self.toggle_notify_perfect)
            self.notify_battery_item = rumps.MenuItem("Low battery", callback=self.toggle_notify_battery)
            self.login_item = rumps.MenuItem("Start at Login", callback=self.toggle_login_item)
            self.handoff_item = rumps.MenuItem("Hand Off to Phone (30 min)", callback=self.toggle_handoff)

            self.menu = [
                self.status_item,
                self.today_item,
                rumps.separator,
                self.battery_item, self.liquid_item, self.current_item, self.target_item, self.eta_item, self.led_item,
                rumps.separator,
                ["Set Temperature", temp_menu],
                ["LED Colour", led_menu],
                ["Schedule", schedule_menu],
                ["Mug Info", info_menu],
                rumps.MenuItem("Drink Log…", callback=self.open_drink_log),
                rumps.separator,
                ["Units", [self.unit_f_item, self.unit_c_item]],
                ["Notifications", [self.notify_perfect_item, self.notify_battery_item]],
                self.login_item,
                rumps.MenuItem("Set Up Mug…", callback=self.open_setup),
                rumps.separator,
                self.handoff_item,
                rumps.MenuItem("Reconnect", callback=self.reconnect),
                rumps.MenuItem("Open Log", callback=self.open_log),
                rumps.MenuItem("Quit", callback=self.quit, key="q"),
            ]
            self._refresh_settings_marks()
            self._render()

            self._startup_timer = rumps.Timer(self._after_launch, 0.5)
            self._startup_timer.start()
            threading.Thread(target=self._run_ble_thread, name="ble", daemon=True).start()

        # ------------------------------------------------------------- startup --

        def _after_launch(self, timer: rumps.Timer) -> None:
            """Runs once the NSApplication loop is up: install the icon, open setup on first run."""
            nsapp = getattr(self, "_nsapp", None)
            item = getattr(nsapp, "nsstatusitem", None)
            if item is None:
                return
            timer.stop()
            if not self.icon_text:
                try:
                    image = NSImage.imageWithSystemSymbolName_accessibilityDescription_("cup.and.saucer.fill", APP_NAME)
                    if image is not None:
                        image.setTemplate_(True)
                        button = item.button()
                        button.setImage_(image)
                        button.setImagePosition_(2)  # NSImageLeft
                        self.prefix = ""
                        self._render()
                except Exception as e:  # noqa: BLE001
                    log.debug("Could not install symbol icon: %s", e)
            if HAVE_WEBKIT:
                try:
                    self._popover = PopoverController.alloc().initWithApp_(self)
                    item.setMenu_(None)
                    button = item.button()
                    button.setTarget_(self._popover)
                    button.setAction_("statusClicked:")
                    button.sendActionOn_((1 << 2) | (1 << 4))  # left mouse up | right mouse up
                    log.info("Popover ready: left-click for the panel, right-click for the menu")
                except Exception:  # noqa: BLE001
                    log.exception("Popover setup failed; using the plain menu")
                    self._popover = None
            if not self.config["mug_address"]:
                self.open_setup(None)

        def show_menu(self) -> None:
            item = self._nsapp.nsstatusitem
            item.setMenu_(self._menu._menu)
            item.button().performClick_(None)
            AppHelper.callAfter(lambda: item.setMenu_(None))

        def panel_html(self) -> str:
            rows = self.tracker.all_rows()
            return drinklog.render_panel_html(rows, self.tracker.daily_summary(rows=rows), self._status_snapshot(), use_f=self.use_f, presets_f=self.config["presets_f"])

        def handle_action_url(self, url: str) -> None:
            path = url[len("embermug://"):].strip("/")
            log.info("Panel action: %s", path)
            if path.startswith("set-temp/"):
                try:
                    self.set_target_c(f_to_c(float(path.split("/", 1)[1])))
                except ValueError:
                    pass
            elif path == "heating-off":
                self.set_target_c(0)
            elif path == "open-log":
                self.open_drink_log(None)
            if self._popover is not None:
                timer = rumps.Timer(lambda t: (t.stop(), self._popover.reload()), 2.0)
                timer.start()

        # --------------------------------------------------------------- units --

        @property
        def use_f(self) -> bool:
            return self.config["temperature_unit"] == "F"

        def fmt_temp(self, temp_c: float, decimals: int = 0) -> str:
            if self.use_f:
                return f"{c_to_f(temp_c):.{decimals}f}°F"
            return f"{temp_c:.{max(decimals, 1)}f}°C"

        def _preset_title(self, label: str, temp_f: int) -> str:
            return f"{label} ({self.fmt_temp(f_to_c(temp_f))})"

        def _rule_title(self, rule: dict[str, Any]) -> str:
            return f"{rule.get('label', 'Rule')}: {rule.get('start', '?')}–{rule.get('end', '?')} → {self.fmt_temp(f_to_c(float(rule.get('temp_f', 0))))}"

        # ----------------------------------------------------------- callbacks --

        def _make_preset_cb(self, temp_f: int):
            return lambda _: self.set_target_c(f_to_c(temp_f))

        def _make_led_cb(self, rgb: tuple[int, int, int]):
            return lambda _: self.set_led(rgb)

        def custom_temperature(self, _) -> None:
            if not self._require_connection():
                return
            unit = "°F" if self.use_f else "°C"
            lo, hi = (MIN_TEMP_F, MAX_TEMP_F) if self.use_f else (MIN_TEMP_C, MAX_TEMP_C)
            current = ""
            if self.data and self.data.target_temp:
                current = f"{c_to_f(self.data.target_temp):.0f}" if self.use_f else f"{self.data.target_temp:.1f}"
            response = rumps.Window(f"Target temperature ({lo:g}–{hi:g}{unit}):", "Set Custom Temperature", default_text=current, ok="Set", cancel="Cancel", dimensions=(300, 24)).run()
            if not response.clicked:
                return
            try:
                value = float(response.text.strip().rstrip("°FCfc "))
            except ValueError:
                rumps.alert("Invalid Input", "Enter a number.")
                return
            temp_c = f_to_c(value) if self.use_f else value
            if not (MIN_TEMP_C - 0.2 <= temp_c <= MAX_TEMP_C + 0.2):
                rumps.alert("Out of Range", f"Ember mugs heat between {lo:g} and {hi:g}{unit}.")
                return
            self.set_target_c(temp_c)

        def heating_off(self, _) -> None:
            if self._require_connection():
                self.set_target_c(0)

        def set_target_c(self, temp_c: float) -> None:
            if not self._require_connection():
                return
            if temp_c != 0:
                temp_c = max(MIN_TEMP_C, min(MAX_TEMP_C, temp_c))
            self._submit(self._write_target(temp_c))

        async def _write_target(self, temp_c: float) -> None:
            assert self.mug is not None
            await self.mug.set_target_temp(temp_c)
            log.info("Target set to %.1fC", temp_c)
            self._render_later()

        def set_led(self, rgb: tuple[int, int, int]) -> None:
            if not self._require_connection():
                return
            self.config["led_colour"] = None if rgb == (255, 255, 255) else list(rgb)
            save_config(self.config)
            self._submit(self._write_led(rgb))

        async def _write_led(self, rgb: tuple[int, int, int]) -> None:
            assert self.mug is not None
            await self.mug.set_led_colour(Colour(*rgb))
            log.info("LED set to %s", rgb)
            self._render_later()

        def set_unit(self, unit: str, render: bool = True) -> None:
            self.config["temperature_unit"] = unit
            save_config(self.config)
            for label, item in self.preset_items.items():
                item.title = self._preset_title(label, self.config["presets_f"][label])
            for item, rule in zip(self.schedule_rule_items, self.config["schedule"]):
                item.title = self._rule_title(rule)
            self._refresh_settings_marks()
            if self.connected and self.config["sync_unit_to_mug"]:
                self._submit(self._sync_unit())
            if render:
                self._render()

        async def _sync_unit(self) -> None:
            if self.mug is None:
                return
            desired = TemperatureUnit.FAHRENHEIT if self.use_f else TemperatureUnit.CELSIUS
            if self.mug.data.temperature_unit != desired:
                await self.mug.set_temperature_unit(desired)
                log.info("Mug display unit set to %s", desired.value)
                self._render_later()

        def rename_mug(self, _) -> None:
            if not self._require_connection():
                return
            current = self.data.name if self.data else ""
            response = rumps.Window("New name (up to 16 characters):", "Rename Mug", default_text=current, ok="Rename", cancel="Cancel", dimensions=(300, 24)).run()
            if response.clicked and response.text.strip():
                self._submit(self._write_name(response.text.strip()))

        async def _write_name(self, name: str) -> None:
            assert self.mug is not None
            await self.mug.set_name(name)
            log.info("Mug renamed to %r", name)
            self._render_later()

        def toggle_notify_perfect(self, _) -> None:
            self.config["notify_on_perfect"] = not self.config["notify_on_perfect"]
            save_config(self.config)
            self._refresh_settings_marks()

        def toggle_notify_battery(self, _) -> None:
            self.config["notify_low_battery"] = not self.config["notify_low_battery"]
            save_config(self.config)
            self._refresh_settings_marks()

        def toggle_auto_schedule(self, _) -> None:
            self.config["auto_schedule"] = not self.config["auto_schedule"]
            save_config(self.config)
            self._refresh_settings_marks()

        def apply_schedule_now(self, _) -> None:
            rule = schedule_rule_for(self.config)
            if rule is None:
                rumps.alert("Schedule", "No schedule rule covers the current time.")
                return
            self.set_target_c(f_to_c(float(rule["temp_f"])))

        def toggle_login_item(self, _) -> None:
            try:
                set_login_item(not login_item_installed())
            except Exception as e:  # noqa: BLE001
                rumps.alert("Start at Login", f"Couldn't update the login item:\n{e}")
            self._refresh_settings_marks()

        def open_setup(self, _) -> None:
            if self._setup_controller is None:
                self._setup_controller = SetupWindowController.alloc().initWithApp_(self)
            self._setup_controller.show()

        def show_history(self, _) -> None:
            if self._history_window is None:
                self._history_window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(NSMakeRect(0, 0, 640, 300), WINDOW_STYLE | 8, NSBackingStoreBuffered, False)
                self._history_window.setTitle_("Ember Mug — Temperature History")
                self._history_window.setReleasedWhenClosed_(False)
                self._history_window.center()
                self._history_view = HistoryView.alloc().initWithFrame_app_(NSMakeRect(0, 0, 640, 300), self)
                self._history_view.setAutoresizingMask_(18)  # width + height sizable
                self._history_window.contentView().addSubview_(self._history_view)
            self._history_view.setNeedsDisplay_(True)
            show_window(self._history_window)

        def open_drink_log(self, _) -> None:
            out = SUPPORT_DIR / "drink-log.html"
            try:
                rows = self.tracker.all_rows()
                snap = self._status_snapshot()
                mug = {k: snap.get(k) for k in ("name", "model", "firmware", "serial")}
                out.write_text(drinklog.render_html(rows, self.tracker.daily_summary(rows=rows), use_f=self.use_f, mug=mug))
                subprocess.Popen(["open", str(out)])
            except Exception as e:  # noqa: BLE001
                log.exception("drink log failed")
                rumps.alert("Drink Log", f"Couldn't build the page:\n{e}")

        def open_config(self, _) -> None:
            if not CONFIG_PATH.exists():
                save_config(self.config)
            subprocess.Popen(["open", "-t", str(CONFIG_PATH)])

        def toggle_handoff(self, _) -> None:
            if self.paused_until > time.time():
                self.take_back()
            else:
                self.hand_off(30 * 60)

        def hand_off(self, seconds: float) -> None:
            """Drop the connection and stay off the mug so the phone's Ember app can take it."""
            self.paused_until = time.time() + seconds
            self.status_text = f"Handed off to phone until {datetime.fromtimestamp(self.paused_until):%H:%M}"
            self.handoff_item.title = "Take Mug Back Now"
            log.info("Hand-off: pausing until %s", datetime.fromtimestamp(self.paused_until).isoformat(timespec="minutes"))
            self._render()
            if self.loop:
                self.loop.call_soon_threadsafe(self._reconnect_requested.set)

        def take_back(self) -> None:
            self.paused_until = 0.0
            self.handoff_item.title = "Hand Off to Phone (30 min)"
            self.reconnect(None)

        def reconnect(self, _) -> None:
            self.paused_until = 0.0
            self.handoff_item.title = "Hand Off to Phone (30 min)"
            self.status_text = "Reconnecting…"
            self._render()
            if self.loop:
                self.loop.call_soon_threadsafe(self._reconnect_requested.set)

        def open_log(self, _) -> None:
            subprocess.Popen(["open", "-a", "Console", str(LOG_PATH)])

        def quit(self, _=None) -> None:
            self._stop.set()
            if self.loop and self.mug:
                future = asyncio.run_coroutine_threadsafe(self.mug.disconnect(), self.loop)
                try:
                    future.result(timeout=3)
                except Exception:  # noqa: BLE001
                    pass
            try:
                STATUS_PATH.unlink()
            except OSError:
                pass
            rumps.quit_application()

        def _require_connection(self) -> bool:
            if self.connected and self.mug is not None:
                return True
            rumps.alert("Not Connected", "The mug isn't connected yet. Wake it (lift it or put it on the coaster) and try again.")
            return False

        def _submit(self, coro) -> None:
            if not self.loop:
                return

            def done(fut: asyncio.Future) -> None:
                try:
                    fut.result()
                except Exception as e:  # noqa: BLE001
                    log.error("Command failed: %s", e)
                    AppHelper.callAfter(rumps.alert, "Mug Error", str(e))

            asyncio.run_coroutine_threadsafe(coro, self.loop).add_done_callback(done)

        # ---------------------------------------------------------- setup mode --

        def enter_setup_mode(self) -> None:
            self.setup_mode = True
            self.status_text = "Setup in progress…"
            self._render()
            if self.loop:
                self.loop.call_soon_threadsafe(self._reconnect_requested.set)

        def leave_setup_mode(self) -> None:
            self.setup_mode = False
            if self.loop:
                self.loop.call_soon_threadsafe(self._reconnect_requested.set)

        def submit_setup_scan(self, callback) -> None:
            async def scan() -> None:
                found: list[tuple[Any, Any]] = []
                error: Optional[str] = None
                try:
                    # Give the main loop a moment to drop any live connection first.
                    for _ in range(20):
                        if not self.connected:
                            break
                        await asyncio.sleep(0.25)
                    async with BleakScanner() as scanner:
                        await asyncio.sleep(10)
                        for device, adv in scanner.discovered_devices_and_advertisement_data.values():
                            is_ember = (device.name or "").startswith("Ember") or EMBER_BLE_SIG in (adv.manufacturer_data or {})
                            if is_ember:
                                found.append((device, adv))
                    found.sort(key=lambda pair: -(getattr(pair[1], "rssi", None) or -999))
                except Exception as e:  # noqa: BLE001
                    error = str(e) or e.__class__.__name__
                    log.warning("Setup scan failed: %s", error)
                AppHelper.callAfter(callback, found, error)

            if self.loop:
                asyncio.run_coroutine_threadsafe(scan(), self.loop)

        # ----------------------------------------------------------------- BLE --

        def _run_ble_thread(self) -> None:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            try:
                self.loop.run_until_complete(self._ble_main())
            finally:
                self.loop.close()

        async def _ble_main(self) -> None:
            backoff = 5
            while not self._stop.is_set():
                self._reconnect_requested.clear()
                if self.setup_mode:
                    # The setup window owns Bluetooth while it's open.
                    await asyncio.sleep(0.5)
                    continue
                if self.paused_until > time.time():
                    await self._run_pending_command()
                    await self._sleep_or_reconnect(min(5, self.paused_until - time.time()))
                    if self.paused_until and self.paused_until <= time.time():
                        AppHelper.callAfter(self.take_back)
                    continue
                try:
                    await self._session()
                    backoff = 5
                except (BleakError, TimeoutError, OSError, asyncio.TimeoutError) as e:
                    self.connected = False
                    reason = str(e).strip() or e.__class__.__name__
                    log.warning("Connection lost: %s", reason)
                    if self._seen_but_refused:
                        # The mug is advertising but won't let us in: something else (the phone) holds it.
                        backoff = min(backoff, 20)
                        self.status_text = f"Mug is connected to another device (your phone?) — retrying in {backoff}s"
                    else:
                        self.status_text = f"Mug not reachable — retrying in {backoff}s"
                    self._render_later()
                    await self._sleep_or_reconnect(backoff)
                    backoff = min(backoff * 2, 60)
                except Exception:  # noqa: BLE001
                    self.connected = False
                    log.exception("Unexpected BLE error")
                    self.status_text = "Error — see log"
                    self._render_later()
                    await self._sleep_or_reconnect(backoff)
                    backoff = min(backoff * 2, 60)

        async def _sleep_or_reconnect(self, seconds: float) -> None:
            try:
                await asyncio.wait_for(self._reconnect_requested.wait(), timeout=seconds)
            except asyncio.TimeoutError:
                pass

        async def _session(self) -> None:
            address = self.config["mug_address"]
            timeout = int(self.config["scan_timeout_seconds"])

            self.status_text = "Searching for mug…"
            self._seen_but_refused = False
            self._render_later()
            device, adv = await find_device(mac=address or None, timeout=timeout)
            if device is None and address:
                log.info("Address %s not seen; scanning for any Ember device", address)
                device, adv = await find_device(timeout=timeout)
            if device is None or adv is None:
                raise BleakError("mug not found (asleep, out of range, or connected to your phone)")
            if device.address != address:
                log.info("Saving mug address %s", device.address)
                self.config["mug_address"] = device.address
                save_config(self.config)

            model_info = get_model_info_from_advertiser_data(adv)
            mug = EmberMug(device, model_info, use_metric=True)
            mug.register_callback(self._on_mug_data)
            self.mug = mug
            self.data = mug.data
            log.info("Found %s (%s) at %s", device.name, model_info.name, device.address)

            self.status_text = "Connecting…"
            self._seen_but_refused = True  # cleared once the connection succeeds
            self._render_later()
            async with mug.connection():
                self._seen_but_refused = False
                self.connected = True
                self.status_text = "Connected"
                log.info("Connected")
                try:
                    await mug.update_initial()
                except Exception as e:  # noqa: BLE001
                    log.debug("update_initial failed (non-fatal): %s", e)
                await mug.update_all()
                await self._read_battery_extras()
                await self._subscribe_statistics(mug)
                await self._set_mug_clock(mug)
                self._last_liquid_state = mug.data.liquid_state
                await self._apply_saved_led()
                if self.config["sync_unit_to_mug"]:
                    await self._sync_unit()
                if self.pending_name:
                    try:
                        await mug.set_name(self.pending_name)
                        log.info("Mug renamed to %r", self.pending_name)
                    except Exception as e:  # noqa: BLE001
                        log.warning("Could not rename mug: %s", e)
                    self.pending_name = None
                self._record_sample()
                self._render_later()

                full_every = float(self.config["full_refresh_seconds"])
                last_full = time.monotonic()
                while not self._stop.is_set() and not self._reconnect_requested.is_set():
                    await asyncio.sleep(1)
                    client = getattr(mug, "_client", None)
                    if client is None or not client.is_connected:
                        raise BleakError("disconnected")
                    await self._run_pending_command()
                    changes = await mug.update_queued_attributes()
                    if time.monotonic() - last_full >= full_every:
                        changes += await mug.update_all()
                        await self._read_battery_extras()
                        last_full = time.monotonic()
                        await self._apply_saved_led()
                        self._record_sample()
                        try:
                            self.tracker.save_state()
                        except Exception:  # noqa: BLE001
                            log.exception("drink tracker save failed")
                    if changes:
                        for change in changes:
                            log.info("%s", change)
                        self._record_sample()
                        self._render_later()

            self.connected = False
            self.mug = None
            log.info("Session ended")
            self._render_later()

        async def _set_mug_clock(self, mug: EmberMug) -> None:
            """Give the mug the time (fc540006: uint32 LE Unix time + int8 UTC offset in hours), as the Ember app does
            on every connect. Reading the characteristic only returns what was written, but once set, the mug's
            on-device drink log stamps every record with absolute time instead of seconds-since-pour (see
            docs/statistics-stream.md)."""
            client = getattr(mug, "_client", None)
            if client is None or not client.is_connected:
                return
            try:
                now = int(time.time())
                offset_h = int(round((datetime.now() - datetime.utcnow()).total_seconds() / 3600))
                payload = now.to_bytes(4, "little") + (offset_h & 0xFF).to_bytes(1, "big")
                await client.write_gatt_char(str(MugCharacteristic.DATE_TIME_AND_ZONE.uuid), payload, response=True)
                log.info("Set mug clock to %d (UTC%+d)", now, offset_h)
            except Exception as e:  # noqa: BLE001
                log.warning("Could not set the mug clock: %s", e)

        async def _subscribe_statistics(self, mug: EmberMug) -> None:
            """Listen to the undocumented 'statistics' characteristic (fc540013) and log every packet
            alongside what the mug was doing, so the format can be worked out offline."""
            client = getattr(mug, "_client", None)
            if client is None or not client.is_connected:
                return
            try:
                await client.start_notify(str(MugCharacteristic.STATISTICS.uuid), self._on_statistics)
                log.info("Subscribed to statistics stream (fc540013); capturing to %s", STATS_CAPTURE_PATH.name)
            except Exception as e:  # noqa: BLE001
                log.warning("Could not subscribe to statistics: %s", e)

        def _on_statistics(self, characteristic: Any, data: bytearray) -> None:
            raw = bytes(data)
            d = self.data
            row = {
                "ts": time.time(),
                "at": datetime.now().isoformat(timespec="seconds"),
                "len": len(raw),
                "hex": raw.hex(" "),
                "state": d.liquid_state.name if d and d.liquid_state is not None else None,
                "current_c": round(d.current_temp, 2) if d else None,
                "target_c": round(d.target_temp, 2) if d else None,
                "level": d.liquid_level if d else None,
                "battery": d.battery.percent if d and d.battery else None,
                "on_base": d.battery.on_charging_base if d and d.battery else None,
                "battery_temp_c": self.battery_temp_c,
            }
            log.info("Statistics packet (%d bytes): %s", len(raw), row["hex"])
            try:
                SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
                with open(STATS_CAPTURE_PATH, "a") as f:
                    f.write(json.dumps(row) + "\n")
            except OSError:
                pass
            try:
                for rec in self.tracker.feed_packet(raw, row["ts"]):
                    log.info("Drink log: t=%ss %s", rec.t, rec.describe(lambda c: f"{c}C" if c is not None else "—"))
            except Exception:  # noqa: BLE001
                log.exception("drink tracker failed on packet")
            self._render_later()

        async def _read_battery_extras(self) -> None:
            """The battery characteristic carries 5 bytes; python-ember-mug only decodes the first two.
            Bytes 2-3 are the battery temperature (x0.01 °C, little-endian) per orlopau's reverse engineering."""
            client = getattr(self.mug, "_client", None)
            if client is None or not client.is_connected:
                return
            try:
                raw = bytes(await client.read_gatt_char(str(MugCharacteristic.BATTERY.uuid)))
            except Exception as e:  # noqa: BLE001
                log.debug("battery raw read failed: %s", e)
                return
            if len(raw) >= 4:
                temp = int.from_bytes(raw[2:4], "little") / 100
                self.battery_temp_c = temp if 0 < temp < 100 else None
            log.debug("battery raw: %s", raw.hex(" "))

        async def _probe(self, command: dict[str, Any]) -> None:
            """Research helpers (--probe): raw reads, the clock write, and the control register. Logged, not shown."""
            client = getattr(self.mug, "_client", None)
            if client is None or not client.is_connected:
                log.warning("probe: not connected")
                return
            base = "fc54{}-236c-4c94-8fa9-944a3e5353fa"
            op = command.get("op")
            if op == "read":
                uuid = base.format(command["char"])
                raw = bytes(await client.read_gatt_char(uuid))
                log.info("probe read %s: %s (%d bytes)", command["char"], raw.hex(" "), len(raw))
            elif op == "set_clock":
                now = int(time.time())
                offset_h = int(round((datetime.now() - datetime.utcnow()).total_seconds() / 3600))
                payload = now.to_bytes(4, "little") + (offset_h & 0xFF).to_bytes(1, "big")
                before = bytes(await client.read_gatt_char(base.format("0006")))
                await client.write_gatt_char(base.format("0006"), payload, response=True)
                after = bytes(await client.read_gatt_char(base.format("0006")))
                log.info("probe set_clock: wrote %s (t=%d, tz=%+d) — before %s, after %s", payload.hex(" "), now, offset_h, before.hex(" "), after.hex(" "))
            elif op == "register":
                addr = int(command["addr"])
                addr_uuid, data_uuid = base.format("0010"), base.format("0011")
                before = bytes(await client.read_gatt_char(addr_uuid))
                await client.write_gatt_char(addr_uuid, bytes([addr]), response=True)
                sel = bytes(await client.read_gatt_char(addr_uuid))
                try:
                    data = bytes(await client.read_gatt_char(data_uuid))
                    log.info("probe register %d: address reads %s, data %s (%d bytes)", addr, sel.hex(" "), data.hex(" "), len(data))
                except Exception as e:  # noqa: BLE001
                    log.info("probe register %d: address reads %s, data read failed: %s", addr, sel.hex(" "), e)
                await client.write_gatt_char(addr_uuid, before, response=True)
            else:
                log.warning("probe: unknown op %s", op)

        async def _dump_gatt(self) -> None:
            """Read every service/characteristic/descriptor on the mug and save it as JSON."""
            if self.mug is None:
                return
            services = await self.mug.discover_services()

            def clean(value: Any) -> Any:
                if isinstance(value, (bytes, bytearray)):
                    return {"hex": bytes(value).hex(" "), "len": len(value)}
                if isinstance(value, Exception):
                    return {"error": str(value)}
                if isinstance(value, dict):
                    return {k: clean(v) for k, v in value.items()}
                if isinstance(value, list):
                    return [clean(v) for v in value]
                return value

            SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
            with open(SUPPORT_DIR / "gatt.json", "w") as f:
                json.dump({"dumped_at": datetime.now().isoformat(timespec="seconds"), "services": clean(services)}, f, indent=2)
            log.info("GATT dump written to %s", SUPPORT_DIR / "gatt.json")

        async def _apply_saved_led(self) -> None:
            wanted = self.config.get("led_colour")
            if not wanted or self.mug is None:
                return
            colour = Colour(*wanted)
            current = self.mug.data.led_colour
            if (current.red, current.green, current.blue) != (colour.red, colour.green, colour.blue):
                try:
                    await self.mug.set_led_colour(colour)
                    log.info("Re-applied LED colour %s", wanted)
                except Exception as e:  # noqa: BLE001
                    log.warning("Could not re-apply LED colour: %s", e)

        async def _maybe_apply_schedule(self, reason: str) -> None:
            if not self.config["auto_schedule"] or self.mug is None:
                return
            rule = schedule_rule_for(self.config)
            if rule is None:
                return
            target_c = max(MIN_TEMP_C, min(MAX_TEMP_C, f_to_c(float(rule["temp_f"]))))
            if abs(self.mug.data.target_temp - target_c) > 0.3:
                await self.mug.set_target_temp(target_c)
                log.info("Schedule (%s): applied %s → %.1fC", reason, rule.get("label"), target_c)

        async def _run_pending_command(self) -> None:
            """Execute a command written by the CLI (--set-temp etc.)."""
            if not COMMAND_PATH.exists():
                return
            try:
                with open(COMMAND_PATH) as f:
                    command = json.load(f)
            except (OSError, json.JSONDecodeError):
                command = None
            try:
                COMMAND_PATH.unlink()
            except OSError:
                pass
            if not command:
                return
            cmd = command.get("cmd")
            log.info("CLI command: %s", command)
            if cmd == "handoff":
                AppHelper.callAfter(self.hand_off, float(command.get("seconds", 1800)))
                return
            if cmd == "takeback":
                AppHelper.callAfter(self.take_back)
                return
            if cmd == "setup":
                AppHelper.callAfter(self.open_setup, None)
                return
            if cmd == "panel":
                if self._popover is not None:
                    AppHelper.callAfter(self._popover.toggle)
                return
            if self.mug is None or not self.connected:
                log.warning("Ignoring %s: mug not connected", cmd)
                return
            try:
                if cmd == "set_temp":
                    temp_c = float(command["temp_c"])
                    if temp_c != 0:
                        temp_c = max(MIN_TEMP_C, min(MAX_TEMP_C, temp_c))
                    await self.mug.set_target_temp(temp_c)
                elif cmd == "led":
                    rgb = tuple(int(v) for v in command["rgb"])
                    self.config["led_colour"] = None if rgb == (255, 255, 255) else list(rgb)
                    save_config(self.config)
                    await self.mug.set_led_colour(Colour(*rgb))
                elif cmd == "refresh":
                    await self.mug.update_all()
                    await self._read_battery_extras()
                elif cmd == "dump_gatt":
                    await self._dump_gatt()
                elif cmd == "probe":
                    await self._probe(command)
                elif cmd == "stats_resync":
                    client = getattr(self.mug, "_client", None)
                    if client is not None:
                        uuid = str(MugCharacteristic.STATISTICS.uuid)
                        try:
                            await client.stop_notify(uuid)
                        except Exception as e:  # noqa: BLE001
                            log.debug("stop_notify: %s", e)
                        await asyncio.sleep(1)
                        await client.start_notify(uuid, self._on_statistics)
                        log.info("Re-subscribed to statistics stream")
            except Exception as e:  # noqa: BLE001
                log.error("CLI command failed: %s", e)
            self._render_later()

        def _on_mug_data(self, data: MugData) -> None:
            self.data = data
            self._check_transitions(data)
            self._record_sample()
            self._render_later()

        def _record_sample(self) -> None:
            data = self.data
            if not data or not self.connected:
                return
            state = data.liquid_state.name if data.liquid_state is not None else ""
            self.history.add(
                data.current_temp, data.target_temp or None, state,
                battery=data.battery.percent if data.battery else None,
                on_base=bool(data.battery and data.battery.on_charging_base),
                level=data.liquid_level,
            )
            self._check_charging_health()
            if data.liquid_state == LiquidState.HEATING and data.target_temp:
                self.eta_seconds = self.history.eta_seconds(data.current_temp, data.target_temp)
            else:
                self.eta_seconds = None

        def _check_charging_health(self) -> None:
            """Warn once if the mug has sat on the coaster for a while without gaining charge."""
            trend = self.history.battery_trend()
            if not trend or not trend["on_base"]:
                self._no_charge_notified = False
                return
            if trend["minutes"] >= 20 and trend["delta"] <= 0 and not getattr(self, "_no_charge_notified", False):
                notify(APP_NAME, f"On the coaster {trend['minutes']:.0f} min with no charge gained — check the coaster is plugged in and the mug is seated.")
                self._no_charge_notified = True

        def _battery_text(self, data: MugData) -> str:
            if not data.battery:
                return "Battery: --"
            text = f"Battery: {data.battery.percent:.0f}%"
            trend = self.history.battery_trend()
            if data.battery.on_charging_base:
                text += " ⚡ charging"
                if trend and trend["on_base"] and trend["minutes"] >= 5:
                    if trend["rate_per_hour"] and trend["rate_per_hour"] > 0:
                        text += f" · +{trend['rate_per_hour']:.0f}%/h"
                    elif trend["minutes"] >= 15:
                        text += f" · no gain in {trend['minutes']:.0f} min ⚠︎"
            elif trend and not trend["on_base"] and trend["rate_per_hour"] and trend["rate_per_hour"] < 0:
                hours_left = data.battery.percent / -trend["rate_per_hour"]
                text += f" · {trend['rate_per_hour']:.0f}%/h (~{hours_left:.1f} h left)"
            return text

        def _check_transitions(self, data: MugData) -> None:
            state = data.liquid_state
            previous = self._last_liquid_state
            if state is not None and state != previous:
                now_ts = time.time()
                try:
                    if state == LiquidState.EMPTY:
                        self.tracker.mark_empty(now_ts)
                    elif previous == LiquidState.EMPTY and state not in (LiquidState.STANDBY,):
                        self.tracker.mark_pour(now_ts)
                except Exception:  # noqa: BLE001
                    log.exception("drink tracker failed on state change")
                if self.config["notify_on_perfect"] and state == LiquidState.TARGET_TEMPERATURE and previous in (LiquidState.HEATING, LiquidState.COOLING):
                    notify(APP_NAME, f"Your drink is ready — {self.fmt_temp(data.current_temp)}")
                # Only a true empty -> filled transition counts; STANDBY is a transient the mug reports mid-drink.
                refilled = previous == LiquidState.EMPTY and state not in (LiquidState.EMPTY, LiquidState.STANDBY)
                if refilled and self.loop:
                    asyncio.run_coroutine_threadsafe(self._maybe_apply_schedule("refill"), self.loop)
                self._last_liquid_state = state

            battery = data.battery
            if battery is not None and self.config["notify_low_battery"]:
                low = float(self.config["low_battery_percent"])
                if battery.percent <= low and not battery.on_charging_base:
                    if not self._low_battery_notified:
                        notify(APP_NAME, f"Mug battery is at {battery.percent:.0f}% — put it on the coaster")
                        self._low_battery_notified = True
                elif battery.on_charging_base or battery.percent > low + 5:
                    self._low_battery_notified = False

        # ------------------------------------------------------------ rendering --

        def _render_later(self) -> None:
            AppHelper.callAfter(self._render)

        def _status_snapshot(self) -> dict[str, Any]:
            data = self.data
            snap: dict[str, Any] = {
                "connected": self.connected,
                "status": self.status_text,
                "address": self.config["mug_address"],
                "unit": self.config["temperature_unit"],
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "updated_ts": time.time(),
            }
            try:
                daily = self.tracker.daily_summary()
                snap["today"] = {k: daily[k] for k in ("date", "cups", "consumed_ml", "first_pour", "last_pour", "avg_time_to_target_s", "abandoned", "in_progress")}
            except Exception:  # noqa: BLE001
                pass
            if data and self.connected:
                snap.update(
                    {
                        "name": data.name,
                        "model": data.model_info.name if data.model_info else None,
                        "serial": data.meta.serial_number if data.meta else None,
                        "firmware": data.firmware.version if data.firmware else None,
                        "battery_percent": data.battery.percent if data.battery else None,
                        "charging": data.battery.on_charging_base if data.battery else None,
                        "liquid_state": data.liquid_state.name.lower() if data.liquid_state is not None else None,
                        "liquid_state_label": data.liquid_state_display,
                        "liquid_level_percent": round(min(100, data.liquid_level / 30 * 100)),
                        "current_c": round(data.current_temp, 2),
                        "current_f": round(c_to_f(data.current_temp), 1),
                        "target_c": round(data.target_temp, 2) if data.target_temp else 0,
                        "target_f": round(c_to_f(data.target_temp), 1) if data.target_temp else 0,
                        "led": data.led_colour.as_hex(),
                        "eta_seconds": round(self.eta_seconds) if self.eta_seconds else None,
                        "battery_trend": self.history.battery_trend(),
                        "battery_temp_c": self.battery_temp_c,
                        "mug_display_unit": data.temperature_unit.value,
                    }
                )
            return snap

        def _write_status_file(self) -> None:
            try:
                SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
                tmp = STATUS_PATH.with_suffix(".tmp")
                with open(tmp, "w") as f:
                    json.dump(self._status_snapshot(), f, indent=2)
                tmp.replace(STATUS_PATH)
            except OSError as e:
                log.debug("status.json write failed: %s", e)

        def _render(self) -> None:
            """Push current state into the menu. Main thread only."""
            data = self.data
            p = self.prefix
            try:
                self.today_item.title = self.tracker.today_line(self.use_f)
            except Exception:  # noqa: BLE001
                pass
            self._write_status_file()
            if self._popover is not None:
                self._popover.maybe_refresh()
            if self._history_view is not None and self._history_window is not None and self._history_window.isVisible():
                self._history_view.setNeedsDisplay_(True)

            model = ""
            if data and data.model_info and data.model_info.model:
                model = f" · {data.model_info.name}"

            if not self.connected or data is None:
                if self.paused_until > time.time():
                    self.title = f"{p}on phone"
                else:
                    self.title = f"{p}…" if "…" in self.status_text else f"{p}--"
                self.status_item.title = f"Status: {self.status_text}"
                self.eta_item.title = "Ready in: --"
                return

            state = data.liquid_state
            state_label = data.liquid_state_display if state is not None else "Unknown"
            temp = self.fmt_temp(data.current_temp)

            if state in (LiquidState.EMPTY, LiquidState.STANDBY):
                self.title = f"{p}Empty"
            elif state == LiquidState.FILLING:
                self.title = f"{p}Filling"
            else:
                arrow = {LiquidState.HEATING: " ▲", LiquidState.COOLING: " ▼", LiquidState.TARGET_TEMPERATURE: " ✓"}.get(state, "")
                self.title = f"{p}{temp}{arrow}"

            self.status_item.title = f"Status: Connected{model}"
            self.battery_item.title = self._battery_text(data)

            level = ""
            if state not in (LiquidState.EMPTY, LiquidState.STANDBY, None) and data.liquid_level:
                level = f" · {min(100, data.liquid_level / 30 * 100):.0f}% full"
            self.liquid_item.title = f"Liquid: {state_label}{level}"
            self.current_item.title = f"Current: {self.fmt_temp(data.current_temp, 1)}"
            self.target_item.title = f"Target: {self.fmt_temp(data.target_temp)}" if data.target_temp else "Target: Heating off"

            if state == LiquidState.HEATING:
                if self.eta_seconds:
                    minutes = max(1, round(self.eta_seconds / 60))
                    self.eta_item.title = f"Ready in: ~{minutes} min"
                else:
                    self.eta_item.title = "Ready in: estimating…"
            elif state == LiquidState.TARGET_TEMPERATURE:
                self.eta_item.title = "Ready in: now ✓"
            else:
                self.eta_item.title = "Ready in: --"

            colour = data.led_colour
            self.led_item.title = f"LED: {colour.as_hex()}"
            current_rgb = (colour.red, colour.green, colour.blue)
            for rgb, item in self.led_items.items():
                item.state = 1 if rgb == current_rgb else 0

            self.info_name_item.title = f"Name: {data.name or '—'}"
            self.info_model_item.title = f"Model: {data.model_info.name if data.model_info else '—'}"
            self.info_serial_item.title = f"Serial: {data.meta.serial_number if data.meta else '—'}"
            if data.firmware:
                self.info_firmware_item.title = f"Firmware: {data.firmware.version} (hw {data.firmware.hardware}, boot {data.firmware.bootloader})"
            self.info_address_item.title = f"Address: {self.config['mug_address']}"
            self.info_unit_item.title = f"Mug display unit: {data.temperature_unit.value}"
            self.info_battery_temp_item.title = f"Battery temperature: {self.fmt_temp(self.battery_temp_c, 1)}" if self.battery_temp_c else "Battery temperature: --"

            active = schedule_rule_for(self.config)
            for item, rule in zip(self.schedule_rule_items, self.config["schedule"]):
                item.state = 1 if rule is active else 0

        def _refresh_settings_marks(self) -> None:
            self.unit_f_item.state = 1 if self.use_f else 0
            self.unit_c_item.state = 0 if self.use_f else 1
            self.notify_perfect_item.state = 1 if self.config["notify_on_perfect"] else 0
            self.notify_battery_item.state = 1 if self.config["notify_low_battery"] else 0
            self.auto_schedule_item.state = 1 if self.config["auto_schedule"] else 0
            self.login_item.state = 1 if login_item_installed() else 0

    setup_logging()
    log.info("Starting %s %s (python %s)", APP_NAME, __version__, sys.version.split()[0])
    # Menu-bar-only: no Dock icon. Done in code rather than via LSUIElement so it works whether
    # we're launched from the .app bundle, a LaunchAgent, or plain `python ember_mug_app.py`.
    NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    EmberMenuBar().run()


# --------------------------------------------------------------------------- #
# CLI (for Raycast, scripts, Claude…)
# --------------------------------------------------------------------------- #


def show_stats_capture() -> int:
    """Print what we've captured from fc540013 so far, with the mug's state at each packet."""
    try:
        rows = [json.loads(line) for line in open(STATS_CAPTURE_PATH) if line.strip()]
    except OSError:
        print(f"No capture yet ({STATS_CAPTURE_PATH}). Leave the app running while you use the mug.")
        return 1
    if not rows:
        print("Capture file is empty so far.")
        return 1
    print(f"{len(rows)} packets captured since {rows[0]['at']}\n")
    print(f"{'time':19} {'len':>3}  {'hex':<60} {'state':10} {'temp':>6} {'lvl':>3} {'bat':>4} base")
    for r in rows[-200:]:
        temp = f"{r['current_c']:.1f}" if r.get("current_c") is not None else "-"
        print(f"{r['at']:19} {r['len']:>3}  {r['hex']:<60} {str(r.get('state')):10} {temp:>6} {str(r.get('level')):>3} {str(r.get('battery')):>4} {r.get('on_base')}")
    lengths: dict[int, int] = {}
    first_bytes: dict[str, int] = {}
    for r in rows:
        lengths[r["len"]] = lengths.get(r["len"], 0) + 1
        first_bytes[r["hex"][:2]] = first_bytes.get(r["hex"][:2], 0) + 1
    print(f"\nPacket lengths: {lengths}")
    print(f"First byte histogram: {first_bytes}")
    return 0


def cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Ember Mug menu bar app / CLI")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    parser.add_argument("--status", action="store_true", help="print the running app's latest status as JSON")
    parser.add_argument("--brief", action="store_true", help="with --status: one line of text instead of JSON")
    parser.add_argument("--set-temp", metavar="TEMP", help="set target temperature (uses the app's unit, e.g. 140 or 140F / 60C)")
    parser.add_argument("--heating-off", action="store_true", help="turn heating off (target 0)")
    parser.add_argument("--led", metavar="COLOUR", help="LED colour: a name (red, blue, white…) or #rrggbb")
    parser.add_argument("--refresh", action="store_true", help="ask the app to re-read everything now")
    parser.add_argument("--setup", action="store_true", help="open the setup window in the running app")
    parser.add_argument("--panel", action="store_true", help="open the instrument panel (the popover) in the running app")
    parser.add_argument("--handoff", nargs="?", const=30, type=float, metavar="MINUTES", help="release the mug so your phone can connect (default 30 min)")
    parser.add_argument("--takeback", action="store_true", help="end a hand-off and reconnect now")
    parser.add_argument("--dump-gatt", action="store_true", help="read every Bluetooth characteristic on the mug into gatt.json (for the curious)")
    parser.add_argument("--stats", action="store_true", help="show the captured packets from the undocumented statistics characteristic")
    parser.add_argument("--stats-resync", action="store_true", help="unsubscribe/resubscribe to the statistics characteristic (triggers the mug's log flush)")
    parser.add_argument("--probe", metavar="OP", help=argparse.SUPPRESS)  # research: set-clock | read:0006 | reg:N
    parser.add_argument("--drink-report", action="store_true", help="decode the captured drink log into an HTML report and open it")
    args = parser.parse_args(argv)

    if args.stats:
        return show_stats_capture()
    if args.drink_report:
        sys.path.insert(0, str(SCRIPT_DIR))
        import drinklog  # noqa: PLC0415

        out = SUPPORT_DIR / "drink-log.html"
        rc = drinklog.main(["--html", str(out)] + ([] if load_config()["temperature_unit"] == "F" else ["--celsius"]))
        if rc == 0:
            subprocess.Popen(["open", str(out)])
        return rc

    if not any([args.status, args.set_temp, args.heating_off, args.led, args.refresh, args.setup, args.panel, args.handoff is not None, args.takeback, args.dump_gatt, args.stats_resync, args.probe]):
        run_app()
        return 0

    status = read_status_file()
    if status is None:
        print("Ember Mug app is not running (no status file). Launch it first.", file=sys.stderr)
        return 2
    age = time.time() - float(status.get("updated_ts", 0))
    if age > 300:
        print(f"Warning: status is {age / 60:.0f} min old — is the app still running?", file=sys.stderr)

    if args.status:
        if args.brief:
            if not status.get("connected"):
                print(f"Mug: {status.get('status', 'not connected')}")
            else:
                unit = status.get("unit", "F")
                cur = status["current_f"] if unit == "F" else status["current_c"]
                tgt = status["target_f"] if unit == "F" else status["target_c"]
                eta = f", ready in ~{round(status['eta_seconds'] / 60)} min" if status.get("eta_seconds") else ""
                charging = " (charging)" if status.get("charging") else ""
                battery = status.get("battery_percent")
                trend = status.get("battery_trend") or {}
                rate = trend.get("rate_per_hour")
                rate_text = f" {rate:+.0f}%/h" if rate else ""
                battery_text = f" · battery {battery:.0f}%{charging}{rate_text}" if battery is not None else ""
                print(f"{cur:g}°{unit} → {tgt:g}°{unit} · {status.get('liquid_state_label')}{eta}{battery_text}")
        else:
            print(json.dumps(status, indent=2))
        return 0 if status.get("connected") else 1

    if args.handoff is not None:
        write_command({"cmd": "handoff", "seconds": args.handoff * 60})
        print(f"Handing the mug off to your phone for {args.handoff:g} min")
        return 0
    if args.takeback:
        write_command({"cmd": "takeback"})
        print("Taking the mug back")
        return 0
    if args.setup:
        write_command({"cmd": "setup"})
        print("Requested setup window")
        return 0
    if args.panel:
        write_command({"cmd": "panel"})
        print("Opening the instrument panel")
        return 0

    if not status.get("connected"):
        print(f"Mug is not connected right now ({status.get('status')}).", file=sys.stderr)
        return 1

    config = load_config()
    if args.set_temp:
        raw = args.set_temp.strip().upper().replace("°", "")
        unit = config["temperature_unit"]
        if raw.endswith("F") or raw.endswith("C"):
            unit, raw = raw[-1], raw[:-1]
        value = float(raw)
        temp_c = f_to_c(value) if unit == "F" else value
        write_command({"cmd": "set_temp", "temp_c": temp_c})
        print(f"Requested target {value:g}°{unit}")
    if args.heating_off:
        write_command({"cmd": "set_temp", "temp_c": 0})
        print("Requested heating off")
    if args.led:
        raw = args.led.strip().lower()
        if raw in ("off", "default"):
            raw = "white"
        if raw.startswith("#") and len(raw) == 7:
            rgb = tuple(int(raw[i : i + 2], 16) for i in (1, 3, 5))
        elif raw in LED_BY_NAME:
            rgb = LED_BY_NAME[raw]
        else:
            print(f"Unknown colour {args.led!r}. Use one of {', '.join(LED_BY_NAME)} or #rrggbb.", file=sys.stderr)
            return 2
        write_command({"cmd": "led", "rgb": list(rgb)})
        print(f"Requested LED {raw}")
    if args.refresh:
        write_command({"cmd": "refresh"})
        print("Requested refresh")
    if args.probe:
        op, _, arg = args.probe.partition(":")
        if op == "set-clock":
            write_command({"cmd": "probe", "op": "set_clock"})
        elif op == "read":
            write_command({"cmd": "probe", "op": "read", "char": arg})
        elif op == "reg":
            write_command({"cmd": "probe", "op": "register", "addr": int(arg)})
        else:
            print(f"unknown probe {args.probe}", file=sys.stderr)
            return 2
        print(f"Probe queued: {args.probe} (see the log)")
        return 0
    if args.stats_resync:
        write_command({"cmd": "stats_resync"})
        print("Requested statistics re-subscribe")
    if args.dump_gatt:
        write_command({"cmd": "dump_gatt"})
        print(f"Requested GATT dump → {SUPPORT_DIR / 'gatt.json'} (takes a few seconds)")
    return 0


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
