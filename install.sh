#!/bin/bash
set -e

echo "======================================"
echo " SmartSSDManager Installation Script"
echo "======================================"

BIN_DIR="$HOME/.local/bin"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_FILE="com.user.smartssd.plist"

mkdir -p "$BIN_DIR"
mkdir -p "$PLIST_DIR"

echo "[1/4] Compiling SmartSSDManager (Swift)..."
swiftc src/SmartSSDManager.swift -o "$BIN_DIR/SmartSSDManager"
echo "✅ Compiled to $BIN_DIR/SmartSSDManager"

echo "[2/4] Copying Settings UI..."
cp src/smartssd_settings.py "$BIN_DIR/"
chmod +x "$BIN_DIR/smartssd_settings.py"
echo "✅ Settings UI copied to $BIN_DIR/smartssd_settings.py"

echo "[3/4] Creating LaunchAgent for autostart..."
cat > "$PLIST_DIR/$PLIST_FILE" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.user.smartssd</string>
    <key>ProgramArguments</key>
    <array>
        <string>$BIN_DIR/SmartSSDManager</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/smartssd.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/smartssd.log</string>
</dict>
</plist>
EOF
echo "✅ LaunchAgent created at $PLIST_DIR/$PLIST_FILE"

echo "[4/4] Starting daemon..."
launchctl unload "$PLIST_DIR/$PLIST_FILE" 2>/dev/null || true
launchctl load "$PLIST_DIR/$PLIST_FILE"
echo "✅ Daemon started!"

echo ""
echo "Installation complete!"
echo "To configure your drives, build the Settings.app wrapper or run the settings script:"
echo "python3 $BIN_DIR/smartssd_settings.py"
