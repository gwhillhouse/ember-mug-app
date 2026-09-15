#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "python-ember-mug>=1.2.1",
# ]
# ///
"""
Find your Ember mug and (optionally) write its address into config.json.

    uv run find_mug.py          # looks for a mug already paired with this Mac
    uv run find_mug.py --pair   # looks for a mug in pairing mode (blue flashing LED)

You normally don't need this: the app scans by name and saves the address
itself. It's here for troubleshooting.
"""

import asyncio
import json
import sys
from pathlib import Path

from ember_mug.scanner import discover_devices, find_device
from ember_mug.utils import get_model_info_from_advertiser_data

CONFIG_PATH = Path(__file__).parent / "config.json"


async def main() -> int:
    pairing = "--pair" in sys.argv
    if pairing:
        print("🔍 Looking for a mug in pairing mode (hold the button 6-8s until the LED flashes blue)…")
        found = await discover_devices(wait=10)
    else:
        print("🔍 Looking for an Ember mug that is awake and paired with this Mac (15s)…")
        device, adv = await find_device(timeout=15)
        found = [(device, adv)] if device else []

    if not found:
        print("❌ No mug found.")
        print("   • Wake it up: lift it or set it on the coaster")
        print("   • Turn Bluetooth OFF on your phone (the mug only talks to one device)")
        print("   • If it was never paired with this Mac, run with --pair")
        return 1

    for device, adv in found:
        model = get_model_info_from_advertiser_data(adv)
        print(f"✅ {device.name or 'Ember device'} — {model.name}")
        print(f"   Address: {device.address}")

    device = found[0][0]
    if input("\nSave this address to config.json? [Y/n] ").strip().lower() in ("", "y", "yes"):
        config = {}
        if CONFIG_PATH.exists():
            try:
                config = json.loads(CONFIG_PATH.read_text())
            except json.JSONDecodeError:
                pass
        config["mug_address"] = device.address
        CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n")
        print(f"📝 Wrote {CONFIG_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
