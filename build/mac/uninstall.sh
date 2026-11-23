#!/bin/bash
# Remove AutoWed from this Mac (run: sudo bash uninstall.sh)
U=$(stat -f%Su /dev/console); UIDN=$(id -u "$U")
launchctl bootout "gui/$UIDN" /Library/LaunchAgents/lk.endora.autowed.engine.plist 2>/dev/null
pkill -f "Contents/MacOS/PoruwaCutEngine" 2>/dev/null
rm -f /Library/LaunchAgents/lk.endora.autowed.engine.plist
rm -rf "/Applications/AutoWed Engine.app" "/Library/Application Support/AutoWed"
UPIA="/Library/Application Support/Adobe/Adobe Desktop Common/RemoteComponents/UPI/UnifiedPluginInstallerAgent/UnifiedPluginInstallerAgent.app/Contents/MacOS/UnifiedPluginInstallerAgent"
[ -x "$UPIA" ] && sudo -u "$U" "$UPIA" --remove "AutoWed" || true
echo "AutoWed removed."
