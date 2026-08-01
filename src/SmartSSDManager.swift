import Cocoa
import Foundation

let configPath = NSHomeDirectory() + "/.local/bin/smartssd_config.json"
var idleCounter = 0

// MARK: — Config

struct Config {
    var volumes: [String]
    var timeoutMinutes: Int
}

func loadConfig() -> Config {
    let defaults = Config(volumes: [], timeoutMinutes: 5)
    guard let data = try? Data(contentsOf: URL(fileURLWithPath: configPath)),
          let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
    else {
        log("⚠️  Could not read config at \(configPath), using defaults.")
        return defaults
    }
    let vols = json["volumes"] as? [String] ?? []
    let timeout = json["timeoutMinutes"] as? Int ?? 5
    return Config(volumes: vols, timeoutMinutes: timeout)
}

// MARK: — Logging

func log(_ msg: String) {
    let formatter = DateFormatter()
    formatter.dateFormat = "yyyy-MM-dd HH:mm:ss"
    print("[\(formatter.string(from: Date()))] \(msg)")
    fflush(stdout)
}

// MARK: — Shell

@discardableResult
func run(_ cmd: String, _ args: [String]) -> (Int, String) {
    let task = Process()
    let pipe = Pipe()
    task.standardOutput = pipe
    task.standardError  = pipe
    task.launchPath  = cmd
    task.arguments   = args
    do { try task.run(); task.waitUntilExit() }
    catch { return (-1, "") }
    let out = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
    return (Int(task.terminationStatus), out)
}

// MARK: — Disk helpers (all by name, never by hardcoded number)

/// Scan `diskutil list` and return the partition id (e.g. disk5s1) for a given volume name.
func findDiskID(byVolumeName name: String) -> String {
    let (_, out) = run("/usr/sbin/diskutil", ["list"])
    for line in out.components(separatedBy: .newlines) where line.contains(name) {
        let parts = line.components(separatedBy: .whitespaces).filter { !$0.isEmpty }
        if let last = parts.last, last.hasPrefix("disk") { return last }
    }
    return ""
}

/// Walk up `Part of Whole` chain, skipping synthesized APFS containers,
/// to find the true physical disk.
func resolvePhysicalDisk(_ diskID: String) -> String {
    var current = diskID
    for _ in 0..<5 {
        let (status, out) = run("/usr/sbin/diskutil", ["info", current])
        guard status == 0 else { break }
        var parent = ""
        var isVirtual = false
        for line in out.components(separatedBy: .newlines) {
            if line.contains("Part of Whole:") || line.contains("APFS Physical Store:") {
                parent = line.components(separatedBy: .whitespaces).filter { !$0.isEmpty }.last ?? ""
            }
            if line.contains("synthesized") || line.contains("Virtual:                   Yes") {
                isVirtual = true
            }
        }
        if parent.isEmpty || parent == current { break }
        if isVirtual || parent.contains("s") {
            // Still inside a container or a partition slice (e.g. disk4s2) — go up again
            current = parent
        } else {
            return parent
        }
    }
    return current
}

// MARK: — Unmount

func unmountAll(volumes: [String]) {
    log("Attempting to unmount: \(volumes.joined(separator: ", "))")

    // Collect all disk IDs; prefer the one that resolves to a real physical disk fastest.
    var physicalDisk = ""
    var fallbackIDs: [String] = []

    for vol in volumes {
        let id = findDiskID(byVolumeName: vol)
        guard !id.isEmpty else { continue }
        fallbackIDs.append(id)
        let phys = resolvePhysicalDisk(id)
        // A real physical disk ID has no 's' after the number (e.g. disk4, not disk4s1).
        if physicalDisk.isEmpty {
            physicalDisk = phys
        }
    }

    if !physicalDisk.isEmpty {
        log("Unmounting physical disk: \(physicalDisk)")
        run("/usr/sbin/diskutil", ["unmountDisk", "force", physicalDisk])
    } else {
        log("Could not resolve physical disk. Unmounting by partition IDs.")
        for id in fallbackIDs { run("/usr/sbin/diskutil", ["unmount", "force", id]) }
    }
}

// MARK: — Idle check

func checkIdle() {
    let cfg = loadConfig()   // re-read config every tick — picks up any changes from Settings UI
    guard !cfg.volumes.isEmpty else { return }

    let fm = FileManager.default
    let mountedVols = cfg.volumes.filter { fm.fileExists(atPath: "/Volumes/\($0)") }

    if mountedVols.isEmpty {
        idleCounter = 0
        return
    }

    var inUse = false
    for vol in mountedVols {
        let (status, _) = run("/usr/sbin/lsof", ["+D", "/Volumes/\(vol)"])
        if status == 0 { inUse = true; break }
    }

    if inUse {
        idleCounter = 0
    } else {
        idleCounter += 1
        log("Idle. Counter: \(idleCounter)/\(cfg.timeoutMinutes) (watching: \(mountedVols.joined(separator: ", ")))")
        if idleCounter >= cfg.timeoutMinutes {
            log("Idle timeout reached.")
            unmountAll(volumes: cfg.volumes)
            idleCounter = 0
        }
    }
}

// MARK: — Sleep watcher

class SleepWatcher: NSObject {
    @objc func willSleep(_ n: Notification) {
        let cfg = loadConfig()
        log("Sleep detected. Unmounting.")
        unmountAll(volumes: cfg.volumes)
    }
}

// MARK: — Entry point

log("SmartSSDManager started. Config: \(configPath)")
let watcher = SleepWatcher()
NSWorkspace.shared.notificationCenter.addObserver(
    watcher,
    selector: #selector(SleepWatcher.willSleep(_:)),
    name: NSWorkspace.willSleepNotification,
    object: nil
)

Timer.scheduledTimer(withTimeInterval: 60.0, repeats: true) { _ in checkIdle() }
RunLoop.main.run()
