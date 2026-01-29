#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "rumps>=0.4.0",
#     "bleak>=0.21.0",
# ]
# ///

"""
Ember Mug Menu Bar App - macOS Control for Ember Smart Mugs

A menu bar application for controlling and monitoring your Ember Mug.
Supports temperature control, LED color changes, and real-time status monitoring.

https://github.com/gwhillhouse/ember-mug-app
"""

import asyncio
import json
import os
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import rumps
from bleak import BleakClient


# Load configuration
def load_config():
    """Load configuration from config.json."""
    config_path = Path(__file__).parent / "config.json"
    
    if not config_path.exists():
        print("❌ config.json not found!")
        print("📝 Create config.json from config.example.json")
        print("   Then run find_mug.py to get your mug's MAC address")
        sys.exit(1)
    
    with open(config_path) as f:
        config = json.load(f)
    
    if not config.get("mug_address") or config["mug_address"] == "YOUR-MUG-MAC-ADDRESS-HERE":
        print("❌ Mug MAC address not configured!")
        print("📝 Run: uv run find_mug.py")
        print("   Then add the MAC address to config.json")
        sys.exit(1)
    
    return config


CONFIG = load_config()
MUG_ADDRESS = CONFIG["mug_address"]
UPDATE_INTERVAL = CONFIG.get("update_interval_seconds", 3)
LED_MAINTAIN_INTERVAL = CONFIG.get("led_maintain_interval_seconds", 5)

# Ember characteristic UUIDs (from reverse engineering)
CHAR_BATTERY = "fc540007-236c-4c94-8fa9-944a3e5353fa"
CHAR_CURRENT_TEMP = "fc540002-236c-4c94-8fa9-944a3e5353fa"
CHAR_TARGET_TEMP = "fc540003-236c-4c94-8fa9-944a3e5353fa"
CHAR_LIQUID_LEVEL = "fc54000d-236c-4c94-8fa9-944a3e5353fa"
CHAR_LIQUID_STATE = "fc54000e-236c-4c94-8fa9-944a3e5353fa"
CHAR_LED_COLOR = "fc540014-236c-4c94-8fa9-944a3e5353fa"
CHAR_CHARGING_BASE = "fc540008-236c-4c94-8fa9-944a3e5353fa"  # Charging status

# Liquid state mapping
LIQUID_STATES = {
    1: "Empty",
    2: "Filling",
    3: "Cold",
    4: "Cooling",
    5: "Heating",
    6: "Perfect",
    7: "Warm"
}


class EmberMenuBar(rumps.App):
    def __init__(self):
        super(EmberMenuBar, self).__init__(
            "🫖",
            title="Starting...",
            quit_button=rumps.MenuItem("Quit", key="q")
        )
        
        self.client: Optional[BleakClient] = None
        self.status = {}
        self.connected = False
        self.ble_loop = None  # Store the event loop for BLE operations
        self.last_led_write = 0  # Timestamp of last LED write
        self.desired_led = None  # The LED color we want to maintain (r, g, b)
        
        # Menu items
        self.menu = [
            rumps.MenuItem("Status: Connecting...", callback=None),
            rumps.separator,
            rumps.MenuItem("Battery: --", callback=None),
            rumps.MenuItem("Current: --", callback=None),
            rumps.MenuItem("Target: --", callback=None),
            rumps.MenuItem("Liquid: --", callback=None),
            rumps.MenuItem("LED: --", callback=None),
            rumps.separator,
            ["Set Temperature", [
                rumps.MenuItem("🥶 120°F (Cold)", callback=lambda _: self.set_temp_f(120)),
                rumps.MenuItem("☕ 130°F (Warm)", callback=lambda _: self.set_temp_f(130)),
                rumps.MenuItem("🔥 140°F (Hot)", callback=lambda _: self.set_temp_f(140)),
                rumps.MenuItem("🌡️ 145°F (Very Hot)", callback=lambda _: self.set_temp_f(145)),
                rumps.MenuItem("🔥 150°F (Max)", callback=lambda _: self.set_temp_f(150)),
                rumps.separator,
                rumps.MenuItem("Custom...", callback=self.set_temp),
            ]],
            ["LED Colors", [
                rumps.MenuItem("⚪ White (Default)", callback=lambda _: self.reset_led()),
                rumps.separator,
                rumps.MenuItem("🔴 Red", callback=lambda _: self.set_led_color(255, 0, 0)),
                rumps.MenuItem("🟢 Green", callback=lambda _: self.set_led_color(0, 255, 0)),
                rumps.MenuItem("🔵 Blue", callback=lambda _: self.set_led_color(0, 0, 255)),
                rumps.MenuItem("🟡 Yellow", callback=lambda _: self.set_led_color(255, 255, 0)),
                rumps.MenuItem("🟣 Magenta", callback=lambda _: self.set_led_color(255, 0, 255)),
                rumps.MenuItem("🔷 Cyan", callback=lambda _: self.set_led_color(0, 255, 255)),
                rumps.MenuItem("🟠 Orange", callback=lambda _: self.set_led_color(255, 165, 0)),
                rumps.MenuItem("🟣 Purple", callback=lambda _: self.set_led_color(128, 0, 128)),
            ]],
            rumps.separator,
            rumps.MenuItem("Reconnect", callback=self.reconnect),
            rumps.separator,
        ]
        
        # Start background BLE thread
        self.ble_thread = threading.Thread(target=self.run_ble_loop, daemon=True)
        self.ble_thread.start()
    
    def run_ble_loop(self):
        """Background thread running async BLE operations."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.ble_loop = loop  # Store for later use
        
        while True:
            try:
                loop.run_until_complete(self.maintain_connection())
            except Exception as e:
                print(f"BLE error: {e}")
                self.connected = False
                self.title = "🫖 ⚠️"
                self.menu["Status: Connecting..."].title = f"Status: Error - {str(e)[:30]}"
            
            # Wait before retrying
            threading.Event().wait(5)
    
    async def maintain_connection(self):
        """Connect and maintain persistent connection to mug."""
        print(f"Connecting to {MUG_ADDRESS}...")
        self.menu["Status: Connecting..."].title = "Status: Connecting..."
        
        async with BleakClient(MUG_ADDRESS, timeout=20.0) as client:
            if not client.is_connected:
                raise Exception("Failed to connect")
            
            self.client = client
            self.connected = True
            print("✅ Connected!")
            self.menu["Status: Connecting..."].title = "Status: ✅ Connected"
            
            # Read data periodically while connected
            while client.is_connected:
                try:
                    await self.read_all_data(client)
                    self.update_menu()
                    
                    # Re-apply desired LED color if set (keeps it from reverting)
                    if self.desired_led and time.time() - self.last_led_write > LED_MAINTAIN_INTERVAL:
                        r, g, b = self.desired_led
                        data = bytes([r, g, b, 255])
                        await client.write_gatt_char(CHAR_LED_COLOR, data)
                        self.last_led_write = time.time()
                        print(f"🔄 Maintaining LED color RGB({r}, {g}, {b})")
                        
                except Exception as e:
                    print(f"Read error: {e}")
                
                await asyncio.sleep(UPDATE_INTERVAL)
            
            print("❌ Disconnected")
            self.connected = False
            self.client = None
    
    async def read_all_data(self, client: BleakClient):
        """Read all sensor data from mug."""
        try:
            # Battery (1 byte, percentage)
            data = await client.read_gatt_char(CHAR_BATTERY)
            self.status['battery'] = data[0]
            
            # Charging base status (1 byte, 0 or 1)
            try:
                data = await client.read_gatt_char(CHAR_CHARGING_BASE)
                self.status['on_charging_base'] = bool(data[0])
            except Exception:
                # If characteristic doesn't exist, assume not charging
                self.status['on_charging_base'] = False
            
            # Current temp (2 bytes, little-endian, divide by 100)
            data = await client.read_gatt_char(CHAR_CURRENT_TEMP)
            temp_raw = struct.unpack('<H', data)[0]
            self.status['current_temp'] = temp_raw / 100.0
            
            # Target temp (2 bytes, little-endian, divide by 100)
            data = await client.read_gatt_char(CHAR_TARGET_TEMP)
            temp_raw = struct.unpack('<H', data)[0]
            self.status['target_temp'] = temp_raw / 100.0
            
            # Liquid state/level - skip for now (complex decoding)
            # These characteristics return encrypted/encoded data that needs
            # proper decoding from the ember-mug library
            self.status['liquid_state'] = "Monitoring"
            self.status['liquid_level'] = 0
            
            # LED Color (4 bytes: RGBA) - don't read if we recently wrote
            # (prevents overwriting our changes)
            if time.time() - self.last_led_write > 10:  # Only read if >10s since last write
                try:
                    data = await client.read_gatt_char(CHAR_LED_COLOR)
                    if len(data) >= 3:
                        r, g, b = data[0], data[1], data[2]
                        self.status['led_color'] = f"#{r:02x}{g:02x}{b:02x}"
                        self.status['led_rgb'] = (r, g, b)
                except Exception as e:
                    print(f"LED color error: {e}")
                    if 'led_color' not in self.status:
                        self.status['led_color'] = None
            
            # Debug: print current reading
            temp_diff = self.status['current_temp'] - self.status['target_temp']
            if abs(temp_diff) < 1:
                status = "At Target 🟢"
            elif temp_diff < 0:
                status = "Heating 🟡"
            else:
                status = "Cooling 🔵"
            print(f"🌡️  {self.status['current_temp']:.1f}°C → {self.status['target_temp']:.1f}°C | {status}")
            
        except Exception as e:
            print(f"Error reading characteristic: {e}")
    
    def update_menu(self):
        """Update menu items with current status."""
        if not self.status:
            return
        
        # Update title bar with temp
        if 'current_temp' in self.status:
            temp_c = self.status['current_temp']
            temp_f = (temp_c * 9/5) + 32
            self.title = f"🫖 {temp_f:.0f}°"
        
        # Update battery
        if 'battery' in self.status:
            battery = self.status['battery']
            on_charger = self.status.get('on_charging_base', False)
            
            if on_charger:
                icon = "⚡"
            elif battery > 20:
                icon = "🔋"
            else:
                icon = "🪫"
            
            charging_text = " (charging)" if on_charger else ""
            self.menu["Battery: --"].title = f"Battery: {battery}%{charging_text} {icon}"
        
        # Update LED color
        if 'led_color' in self.status and self.status['led_color']:
            self.menu["LED: --"].title = f"LED: {self.status['led_color']} 💡"
        
        # Update current temp
        if 'current_temp' in self.status:
            temp_c = self.status['current_temp']
            temp_f = (temp_c * 9/5) + 32
            self.menu["Current: --"].title = f"Current: {temp_c:.1f}°C ({temp_f:.0f}°F)"
        
        # Update target temp
        if 'target_temp' in self.status:
            temp_c = self.status['target_temp']
            temp_f = (temp_c * 9/5) + 32
            self.menu["Target: --"].title = f"Target: {temp_c:.1f}°C ({temp_f:.0f}°F)"
        
        # Update liquid state - simplified for now
        temp_diff = abs(self.status.get('current_temp', 0) - self.status.get('target_temp', 0))
        if temp_diff < 1:
            state = "At Target 🟢"
        elif self.status.get('current_temp', 0) < self.status.get('target_temp', 0):
            state = "Heating 🟡"
        else:
            state = "Cooling 🔵"
        
        self.menu["Liquid: --"].title = f"Status: {state}"
    
    def set_temp_f(self, temp_f):
        """Set temperature directly (called from menu)."""
        if not self.connected:
            print("Not connected, can't set temp")
            return
        
        temp_c = (temp_f - 32) * 5/9
        
        # Write target temp to mug using the BLE loop
        if self.ble_loop:
            asyncio.run_coroutine_threadsafe(
                self._set_temp_async(temp_c),
                self.ble_loop
            )
            self.title = "🫖 ..."
            print(f"Setting temp to {temp_f}°F ({temp_c:.1f}°C)...")
    
    @rumps.clicked("Custom...")
    def set_temp(self, _):
        """Dialog to set custom temperature (for advanced users)."""
        if not self.connected:
            rumps.alert("Not Connected", "Wait for mug to connect first.")
            return
        
        current_target = self.status.get('target_temp', 62.8)
        current_target_f = (current_target * 9/5) + 32
        
        # Simple alert with instructions (no editable field due to rumps bug)
        response = rumps.Window(
            f"Current: {current_target_f:.0f}°F\n\nEnter new target (32-212°F):\n\n(Click in text field first, then delete and type)",
            "Set Custom Temperature",
            default_text=f"{current_target_f:.0f}",
            ok="Set",
            cancel="Cancel",
            dimensions=(340, 100)
        ).run()
        
        if response.clicked:
            try:
                temp_f = float(response.text)
                if temp_f < 32 or temp_f > 212:
                    rumps.alert("Invalid Temperature", "Please enter between 32°F and 212°F")
                    return
                
                self.set_temp_f(temp_f)
                
            except ValueError:
                rumps.alert("Invalid Input", "Please enter a valid number.")
    
    async def _set_temp_async(self, temp_c):
        """Actually write the temperature to the mug."""
        if self.client and self.client.is_connected:
            try:
                # Convert to int16 * 100
                temp_raw = int(temp_c * 100)
                data = struct.pack('<H', temp_raw)
                await self.client.write_gatt_char(CHAR_TARGET_TEMP, data)
                print(f"✅ Set target temp to {temp_c:.1f}°C")
            except Exception as e:
                print(f"❌ Failed to set temp: {e}")
    
    def reset_led(self):
        """Reset LED to default white and stop maintaining custom color."""
        self.desired_led = None  # Stop maintaining custom color
        self.set_led_color(255, 255, 255)
        print("Reset LED to default white")
    
    def set_led_color(self, r, g, b):
        """Set LED color directly (called from menu)."""
        if not self.connected:
            print("Not connected, can't set LED")
            return
        
        # Store desired color so we keep re-applying it (unless it's white)
        if (r, g, b) != (255, 255, 255):
            self.desired_led = (r, g, b)
        
        # Write LED color using the BLE loop
        if self.ble_loop:
            asyncio.run_coroutine_threadsafe(
                self._set_led_async(r, g, b),
                self.ble_loop
            )
            if self.desired_led:
                print(f"Setting LED to RGB({r}, {g}, {b}) and will maintain it...")
            else:
                print(f"Setting LED to RGB({r}, {g}, {b})")
    
    async def _set_led_async(self, r, g, b):
        """Actually write the LED color to the mug."""
        if self.client and self.client.is_connected:
            try:
                # RGBA format (alpha = 255)
                data = bytes([r, g, b, 255])
                await self.client.write_gatt_char(CHAR_LED_COLOR, data)
                print(f"✅ Set LED to RGB({r}, {g}, {b})")
                
                # Update our status immediately so we don't overwrite it
                self.status['led_color'] = f"#{r:02x}{g:02x}{b:02x}"
                self.status['led_rgb'] = (r, g, b)
                self.last_led_write = time.time()  # Mark when we wrote
                
                # Force a menu update
                self.update_menu()
            except Exception as e:
                print(f"❌ Failed to set LED: {e}")
    
    @rumps.clicked("Reconnect")
    def reconnect(self, _):
        """Force reconnection."""
        self.title = "🫖 ..."
        if self.client and self.client.is_connected and self.ble_loop:
            # Will trigger reconnect in the loop
            asyncio.run_coroutine_threadsafe(
                self.client.disconnect(),
                self.ble_loop
            )


if __name__ == "__main__":
    print("Starting Ember Menu Bar App...")
    print("Make sure mug is powered on and nearby!")
    EmberMenuBar().run()
