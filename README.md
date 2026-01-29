# Ember Mug Menu Bar App 🫖

A native macOS menu bar application for controlling and monitoring your Ember Smart Mug via Bluetooth.

![Menu Bar Icon](https://img.shields.io/badge/macOS-10.15%2B-blue)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

## Features

- 🌡️ **Real-time temperature monitoring** - Updates every 3 seconds
- 🔋 **Battery status** - Shows charge level and charging state
- 🎯 **Quick temperature presets** - 120°F, 130°F, 140°F, 145°F, 150°F
- 🎨 **LED color control** - 9 colors with persistent mode
- 📊 **Live menu bar display** - Current temp shown in menu bar
- 🔄 **Auto-reconnect** - Handles connection drops gracefully
- 💤 **No Terminal required** - Runs as a native macOS app

## Screenshots

### Menu Bar
```
🫖 145° 
├── Status: ✅ Connected
├── Battery: 78% (charging) ⚡
├── Current: 62.8°C (145°F)
├── Target: 62.8°C (145°F)
├── Status: At Target 🟢
├── LED: #ffffff 💡
├──────────
├── Set Temperature ▶
│   ├── 🥶 120°F (Cold)
│   ├── ☕ 130°F (Warm)
│   ├── 🔥 140°F (Hot)
│   ├── 🌡️ 145°F (Very Hot)
│   └── 🔥 150°F (Max)
├── LED Colors ▶
│   ├── ⚪ White (Default)
│   ├── 🔴 Red
│   ├── 🟢 Green
│   └── ... (9 colors total)
└── Quit
```

## Requirements

- **macOS 10.15+** (Catalina or newer)
- **Python 3.10+**
- **Ember Mug** (Ceramic Mug, Mug 2, Cup, Tumbler, or Travel Mug)
- **Bluetooth** enabled on your Mac

## Installation

### 1. Install UV (Python package manager)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Or via Homebrew:
```bash
brew install uv
```

### 2. Clone this repository

```bash
git clone https://github.com/gwhillhouse/ember-mug-app.git
cd ember-mug-app
```

### 3. Find your mug's MAC address

Put your Ember Mug in pairing mode:
- Hold the button on the bottom for 6-8 seconds
- LED should flash **blue**

Then run the discovery script:
```bash
uv run find_mug.py
```

Copy the MAC address shown.

### 4. Create configuration

```bash
cp config.example.json config.json
```

Edit `config.json` and replace `YOUR-MUG-MAC-ADDRESS-HERE` with your mug's MAC address.

### 5. Build the app

```bash
chmod +x build_app.sh
./build_app.sh
```

This creates `Ember Mug.app` in `~/Applications/`

### 6. Launch!

Double-click `Ember Mug.app` in your Applications folder.

The 🫖 icon will appear in your menu bar!

## Usage

### First Time Setup

1. **Disconnect your iPhone** - Turn OFF Bluetooth on your iPhone (Settings → Bluetooth → OFF)
   - The mug can only connect to one device at a time
   - Once connected to your Mac, you can turn iPhone Bluetooth back on

2. **Put mug in pairing mode** (if needed)
   - Hold button 6-8 seconds until blue LED flashes
   - Tap button once when "Connected" appears in the menu

3. **That's it!** The app will maintain the connection automatically

### Changing Temperature

Click **"Set Temperature"** in the menu and choose a preset:
- 🥶 **120°F** - Cold brew / iced drinks
- ☕ **130°F** - Warm coffee
- 🔥 **140°F** - Hot coffee
- 🌡️ **145°F** - Very hot (default)
- 🔥 **150°F** - Maximum heat

Or click **"Custom..."** to enter a specific temperature.

### Changing LED Color

Click **"LED Colors"** and pick a color:
- The color will persist (re-applied every 5 seconds)
- Click **"White (Default)"** to reset to standard white

### Auto-start at Login

1. System Settings → General → Login Items
2. Click **+** button
3. Navigate to `~/Applications/Ember Mug.app`
4. Add it to the list

## Troubleshooting

### App won't connect

- ✅ Check Bluetooth is ON (System Settings → Bluetooth)
- ✅ Put mug in pairing mode (hold button 6-8s, blue LED)
- ✅ Make sure mug is powered on (has liquid or on charger)
- ✅ Check iPhone Bluetooth is OFF (or forget mug on iPhone)
- ✅ Try clicking "Reconnect" in the menu

### LED color keeps reverting to white

- Turn OFF Bluetooth on your iPhone
- The Ember app on iPhone will override LED changes
- Use "Forget This Device" on iPhone if you want Mac-only control

### Menu bar icon missing

- Check logs: `tail -f /tmp/ember-mug.log`
- Quit and relaunch the app
- Make sure `config.json` has the correct MAC address

### Permission errors

The app needs Bluetooth permissions. When prompted:
1. System Settings → Privacy & Security → Bluetooth
2. Enable for "Python" or "ember_mug_app"

## Development

### Running from source

```bash
uv run ember_mug_app.py
```

### Project structure

```
ember-mug-app/
├── ember_mug_app.py      # Main application
├── find_mug.py           # MAC address discovery tool
├── build_app.sh          # App bundle builder
├── config.json           # Your config (not tracked)
├── config.example.json   # Example config
└── README.md
```

### Configuration options

`config.json`:
```json
{
  "mug_address": "XX:XX:XX:XX:XX:XX",
  "update_interval_seconds": 3,
  "led_maintain_interval_seconds": 5
}
```

## How It Works

This app connects directly to your Ember Mug via Bluetooth Low Energy (BLE) using the reverse-engineered Ember protocol. It:

1. Maintains a persistent BLE connection
2. Reads sensor data every 3 seconds (temp, battery, LED)
3. Provides write access to change temperature and LED
4. Re-applies LED color every 5 seconds to prevent reversion

**Note:** This is an unofficial app. It works by reverse-engineering the Ember BLE protocol and is not affiliated with Ember.

## Credits

- BLE protocol reverse engineering: [python-ember-mug](https://github.com/sopelj/python-ember-mug)
- Built with [rumps](https://github.com/jaredks/rumps) (macOS menu bar framework)
- Created by [@gwhillhouse](https://github.com/gwhillhouse)

## License

MIT License - see [LICENSE](LICENSE) file for details

## Contributing

Contributions welcome! Please open an issue or PR.

### Ideas for future features
- [ ] Battery low/full notifications
- [ ] Temperature alerts ("Your coffee is ready!")
- [ ] Time-based automation (cooler at night)
- [ ] LED effects (fade, pulse, rainbow)
- [ ] Multiple mug support
- [ ] Keyboard shortcuts

---

**Enjoy your smart mug! 🫖✨**

Questions? Open an issue on GitHub.
