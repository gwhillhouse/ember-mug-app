#!/bin/bash

# Raycast Script Command: show the mug's status (and optionally set a temperature).
# Add this folder as a Script Directory in Raycast → Extensions → Script Commands.

# @raycast.schemaVersion 1
# @raycast.title Ember Mug
# @raycast.mode inline
# @raycast.refreshTime 30s
# @raycast.packageName Ember
# @raycast.icon ☕
# @raycast.argument1 { "type": "text", "placeholder": "temp (optional)", "optional": true }

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${EMBER_MUG_PYTHON:-$HOME/.local/pipx/venvs/python-ember-mug/bin/python}"
[ -x "$PYTHON" ] || PYTHON="$APP_DIR/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="python3"

if [ -n "$1" ]; then
    "$PYTHON" "$APP_DIR/ember_mug_app.py" --set-temp "$1" >/dev/null && sleep 1
fi
"$PYTHON" "$APP_DIR/ember_mug_app.py" --status --brief 2>&1
