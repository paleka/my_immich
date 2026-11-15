#!/bin/bash
# install.sh — builds the clickable Mac app with a menu-bar icon.
#
#   bash install.sh
#
# Creates:
#   ~/Applications/my_immich.app                   the app you launch
#   ~/Library/Application Support/my_immich/       a private Python environment
#                                                  (with the `rumps` menu-bar
#                                                  package) and a copy of my_immich.py
# Nothing is installed system-wide. Run it again after changing my_immich.py.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/my_immich.py"
SUPPORT="$HOME/Library/Application Support/my_immich"
APP="$HOME/Applications/my_immich.app"

[[ "$(uname)" == "Darwin" ]] || { echo "This installer is for macOS."; exit 1; }
[[ -f "$SRC" ]] || { echo "my_immich.py must be in the same folder as install.sh."; exit 1; }
[[ -d "$HERE/assets" ]] || { echo "The assets folder must be next to install.sh."; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required (xcode-select --install, or brew install python)."; exit 1; }

echo "==> Setting up the private Python environment"
mkdir -p "$SUPPORT"
[[ -x "$SUPPORT/venv/bin/python" ]] || python3 -m venv "$SUPPORT/venv"
"$SUPPORT/venv/bin/python" -m pip install --quiet --upgrade pip
"$SUPPORT/venv/bin/python" -m pip install --quiet -r "$HERE/requirements.txt"
cp "$SRC" "$SUPPORT/my_immich.py"
rm -rf "$SUPPORT/assets"
cp -R "$HERE/assets" "$SUPPORT/assets"      # menu-bar icon lives here

echo "==> Building $APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# App icon: build AppIcon.icns from assets/icon.png with the tools macOS ships.
ICONSET="$(mktemp -d)/AppIcon.iconset"
mkdir -p "$ICONSET"
if command -v sips >/dev/null && command -v iconutil >/dev/null; then
  for size in 16 32 128 256 512; do
    sips -z "$size" "$size" "$HERE/assets/icon.png" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
    sips -z "$((size * 2))" "$((size * 2))" "$HERE/assets/icon.png" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
  done
  iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns" \
    || echo "   (could not build the app icon; the app still works)"
else
  echo "   (sips/iconutil not found; skipping the app icon)"
fi
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>my_immich</string>
  <key>CFBundleDisplayName</key><string>my_immich</string>
  <key>CFBundleIdentifier</key><string>local.my-immich</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>launcher</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSUIElement</key><true/>
</dict>
</plist>
PLIST

cat > "$APP/Contents/MacOS/launcher" <<'LAUNCH'
#!/bin/bash
SUPPORT="$HOME/Library/Application Support/my_immich"
mkdir -p "$HOME/Library/Logs"
exec "$SUPPORT/venv/bin/python" "$SUPPORT/my_immich.py" --tray \
  >> "$HOME/Library/Logs/my_immich.log" 2>&1
LAUNCH
chmod +x "$APP/Contents/MacOS/launcher"
touch "$APP"                                 # nudge Finder to pick up the new icon

echo
echo "Done. Open it with:  open \"$APP\""
echo "A card-shaped icon appears in the menu bar and the page opens in your browser."
echo "The page's Setup tab walks through the remaining steps."
echo "Start at login: System Settings > General > Login Items > add my_immich."
echo "Log file: ~/Library/Logs/my_immich.log"
