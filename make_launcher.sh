#!/usr/bin/env bash
# Quick alternative to build_app.sh: creates a tiny "MacBook Optimizer.app" that runs THIS
# folder's code with THIS folder's .venv. Takes a second, but the app stops working if you
# move or delete the project folder. Usage:  ./make_launcher.sh [destination-folder]
set -euo pipefail
cd "$(dirname "$0")"
PROJECT="$(pwd)"
DEST="${1:-$HOME/Desktop}"
APP="$DEST/MacBook Optimizer.app"
VENV_PY="$PROJECT/.venv/bin/python3"

if [[ ! -x "$VENV_PY" ]]; then
  echo "No .venv found in $PROJECT - create it first (see the README)."; exit 1
fi

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp assets/AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>MacBook Optimizer</string>
  <key>CFBundleDisplayName</key><string>MacBook Optimizer</string>
  <key>CFBundleIdentifier</key><string>com.macbookoptimizer.launcher</string>
  <key>CFBundleExecutable</key><string>launcher</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppleEventsUsageDescription</key>
  <string>MacBook Optimizer uses Finder to move files to the Trash and System Events to list your Login Items.</string>
</dict>
</plist>
PLIST

cat > "$APP/Contents/MacOS/launcher" <<SH
#!/bin/bash
cd "$PROJECT"
exec "$VENV_PY" "$PROJECT/macbook_optimizer.py"
SH
chmod +x "$APP/Contents/MacOS/launcher"
touch "$APP"

echo "Created: $APP"
