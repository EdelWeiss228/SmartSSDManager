#!/bin/bash
set -euo pipefail

echo "======================================"
echo " SmartSSDManager Installation Script"
echo "======================================"

BIN_DIR="$HOME/.local/bin"
LOG_DIR="$HOME/Library/Logs"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_FILE="com.user.smartssd.plist"
LABEL="com.user.smartssd"

mkdir -p "$BIN_DIR" "$PLIST_DIR" "$LOG_DIR"

echo "[1/5] Compiling SmartSSDManager (Swift)..."
swiftc -O src/SmartSSDManager.swift -o "$BIN_DIR/SmartSSDManager"
echo "✅ Compiled to $BIN_DIR/SmartSSDManager"

echo "[2/5] Copying Settings UI..."
cp src/smartssd_settings.py "$BIN_DIR/"
chmod +x "$BIN_DIR/smartssd_settings.py"
echo "✅ Settings UI copied to $BIN_DIR/smartssd_settings.py"

echo "[3/5] Creating LaunchAgent..."
# Remove leftovers from a previous "disabled" state.
rm -f "$PLIST_DIR/$PLIST_FILE.disabled"
cat > "$PLIST_DIR/$PLIST_FILE" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$BIN_DIR/SmartSSDManager</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/SmartSSDManager.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/SmartSSDManager.log</string>
</dict>
</plist>
EOF
plutil -lint "$PLIST_DIR/$PLIST_FILE" >/dev/null
echo "✅ LaunchAgent created at $PLIST_DIR/$PLIST_FILE"

echo "[4/5] Reloading LaunchAgent..."
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl enable "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST_DIR/$PLIST_FILE"
echo "✅ LaunchAgent loaded"

echo "[5/5] Verifying daemon..."
sleep 1
if launchctl print "gui/$UID/$LABEL" >/dev/null 2>&1; then
    echo "✅ Daemon is running (PID: $(launchctl print "gui/$UID/$LABEL" | awk '/pid =/{print $3; exit}'))"
else
    echo "❌ Daemon did not start. Check $LOG_DIR/SmartSSDManager.log"
    exit 1
fi

echo ""
echo "Installation complete!"
echo "Log:     $LOG_DIR/SmartSSDManager.log"
echo "Config:  $BIN_DIR/smartssd_config.json"
echo "Check:   $BIN_DIR/SmartSSDManager --check"
echo ""
echo "To configure drives, run:"
echo "  python3 $BIN_DIR/smartssd_settings.py"
