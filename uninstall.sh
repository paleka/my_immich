#!/bin/bash
# uninstall.sh — removes the app and its private Python environment.
#
#   bash uninstall.sh            keep your settings
#   bash uninstall.sh --all      also remove ~/.config/my_immich (settings + API key)
#
# Quit my_immich from the menu bar first.

set -euo pipefail

APP="$HOME/Applications/my_immich.app"
SUPPORT="$HOME/Library/Application Support/my_immich"
CONFIG="$HOME/.config/my_immich"

rm -rf "$APP" "$SUPPORT"
echo "Removed $APP"
echo "Removed $SUPPORT"

if [[ "${1:-}" == "--all" ]]; then
  rm -rf "$CONFIG"
  echo "Removed $CONFIG"
else
  echo "Kept settings in $CONFIG (run with --all to remove them too)."
fi
