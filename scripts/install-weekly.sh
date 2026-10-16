#!/bin/zsh
# Install (or with --uninstall, remove) the weekly launchd job for this checkout.
set -eu
REPO="${0:A:h:h}"
LABEL="com.geo-unblocked.weekly"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
if [[ "${1:-}" == "--uninstall" ]]; then
  rm -f "$PLIST"; echo "removed $LABEL"; exit 0
fi
mkdir -p "$HOME/Library/LaunchAgents" "$REPO/work/logs"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>/bin/zsh</string><string>$REPO/scripts/weekly.sh</string></array>
  <key>StartCalendarInterval</key><dict><key>Weekday</key><integer>${GEO_UNBLOCKED_WEEKDAY:-0}</integer><key>Hour</key><integer>${GEO_UNBLOCKED_HOUR:-4}</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>$REPO/work/logs/launchd.log</string>
  <key>StandardErrorPath</key><string>$REPO/work/logs/launchd.log</string>
</dict>
</plist>
PL
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed $LABEL: runs $REPO/scripts/weekly.sh every week (weekday ${GEO_UNBLOCKED_WEEKDAY:-0}, 0 = Sunday) at ${GEO_UNBLOCKED_HOUR:-4}:00"
