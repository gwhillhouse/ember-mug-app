#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "bleak>=0.21.0",
# ]
# ///

"""
Find your Ember Mug's Bluetooth MAC address.

Put your mug in pairing mode (hold button 6-8 seconds until blue LED flashes)
then run this script.
"""

import asyncio
from bleak import BleakScanner


async def main():
    print("🔍 Scanning for Ember Mugs...")
    print("Make sure your mug is in pairing mode (blue LED flashing)\n")
    
    devices = await BleakScanner.discover(timeout=10.0)
    
    ember_devices = []
    other_devices = []
    
    for device in devices:
        if device.name and "ember" in device.name.lower():
            ember_devices.append(device)
        elif device.name:
            other_devices.append(device)
    
    if ember_devices:
        print("✅ Found Ember Mug(s):\n")
        for device in ember_devices:
            print(f"  Name: {device.name}")
            print(f"  MAC Address: {device.address}")
            print(f"  Signal: {getattr(device, 'rssi', 'N/A')} dBm")
            print()
        
        print("📝 Copy the MAC Address above into config.json")
        print("   (Create config.json from config.example.json)")
    else:
        print("❌ No Ember Mugs found.\n")
        print("Troubleshooting:")
        print("  1. Put mug in pairing mode (hold button 6-8 seconds)")
        print("  2. LED should flash BLUE")
        print("  3. Make sure mug is powered on (has liquid or on charger)")
        print("  4. Check mug is within range (~30 feet)")
        
        if other_devices:
            print(f"\n📡 Found {len(other_devices)} other Bluetooth devices nearby")
            print("   (Ember mug not detected)")


if __name__ == "__main__":
    asyncio.run(main())
