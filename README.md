# macOS Disk Mounter (SmartSSDManager)

A background daemon and web-based settings UI for macOS that intelligently manages external SSDs and their partitions. It automatically unmounts drives when they are idle or when the Mac goes to sleep, preventing the infamous "Disk Not Ejected Properly" errors on flaky USB-C connections.

## Features
- **Auto-Unmount on Sleep:** Instantly unmounts your physical external drives right before the system goes to sleep.
- **Idle Timeout:** Automatically unmounts drives if no files are being accessed for a specified amount of time.
- **Robust Physical Disk Resolution:** Properly unmounts the entire physical disk (e.g., `disk4`) rather than just virtual APFS containers (`disk5`), ensuring the drive is truly safe to unplug.
- **Web UI Settings:** A modern, dark-mode friendly web interface (running locally via Python) to add/remove volumes, change the idle timer, and automatically generate smart desktop shortcuts.
- **Smart Shortcuts:** Automatically generated macOS `.app` shortcuts for your desktop that mount the drive (if it was auto-ejected) and open it in Finder.

## Components
- `SmartSSDManager.swift` - A lightweight Swift daemon that runs in the background.
- `smartssd_settings.py` - A Python script running a local HTTP server for settings management.

## Installation

1. Clone this repository:
   ```bash
   git clone https://github.com/EdelWeiss228/macOS-disk-mouter.git
   cd macOS-disk-mouter
   ```
2. Make the install script executable and run it:
   ```bash
   chmod +x install.sh
   ./install.sh
   ```

This will:
- Compile the Swift daemon.
- Install the binaries to `~/.local/bin`.
- Register the daemon with `launchd` so it starts automatically on login.

## Usage

To configure the daemon, run the settings UI:
```bash
python3 ~/.local/bin/smartssd_settings.py
```
This will open a settings panel in your web browser where you can:
- Add volume names (as seen in `diskutil list`) to track.
- Set the idle timeout duration.
- The UI will automatically create Desktop shortcuts for added volumes.
