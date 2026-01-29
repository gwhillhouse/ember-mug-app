#!/bin/bash
# Build Ember Mug.app bundle

set -e  # Exit on error

APP_NAME="Ember Mug"
APP_DIR="$HOME/Applications/$APP_NAME.app"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "🫖 Building $APP_NAME.app..."

# Check config exists
if [ ! -f "$SCRIPT_DIR/config.json" ]; then
    echo "❌ config.json not found!"
    echo "📝 Create config.json from config.example.json"
    echo "   Then run: uv run find_mug.py"
    exit 1
fi

# Remove old app if exists
rm -rf "$APP_DIR"

# Create app bundle structure
mkdir -p "$APP_DIR/Contents/MacOS"
mkdir -p "$APP_DIR/Contents/Resources"

# Create Info.plist
cat > "$APP_DIR/Contents/Info.plist" << 'PLISTEOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>ember-launcher</string>
    <key>CFBundleIdentifier</key>
    <string>com.ember.mugmenubar</string>
    <key>CFBundleName</key>
    <string>Ember Mug</string>
    <key>CFBundleDisplayName</key>
    <string>Ember Mug</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0.0</string>
    <key>CFBundleVersion</key>
    <string>1</string>
    <key>LSMinimumSystemVersion</key>
    <string>10.15</string>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
PLISTEOF

# Create launcher script (runs without Terminal)
cat > "$APP_DIR/Contents/MacOS/ember-launcher" << LAUNCHEREOF
#!/bin/bash

# Kill any existing instance
pkill -f ember_mug_app.py 2>/dev/null
sleep 0.5

# Change to script directory
cd "$SCRIPT_DIR"

# Launch in background with nohup, detached from parent
nohup /opt/homebrew/bin/uv run ember_mug_app.py > /tmp/ember-mug.log 2>&1 &

# Exit immediately so app doesn't wait
exit 0
LAUNCHEREOF

chmod +x "$APP_DIR/Contents/MacOS/ember-launcher"

echo "✅ Created: $APP_DIR"
echo ""
echo "To launch:"
echo "  1. Open Finder"
echo "  2. Go to ~/Applications"
echo "  3. Double-click 'Ember Mug.app'"
echo ""
echo "💡 Tips:"
echo "  - Add to Dock: Drag app to Dock"
echo "  - Auto-start: System Settings → General → Login Items"
echo "  - View logs: tail -f /tmp/ember-mug.log"
echo ""
echo "🎉 Ready to use!"
