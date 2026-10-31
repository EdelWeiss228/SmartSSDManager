import Cocoa
import Foundation

// MARK: - Runtime

let fm = FileManager.default
let home = fm.homeDirectoryForCurrentUser.path
let defaultConfigPath = home + "/.local/bin/smartssd_config.json"
let configPath = ProcessInfo.processInfo.environment["SMARTSSD_CONFIG"] ?? defaultConfigPath

// Internal knobs (used by tests; production defaults are 60 s / config minutes).
let tickInterval = max(1.0, Double(ProcessInfo.processInfo.environment["SMARTSSD_TICK_SECONDS"] ?? "") ?? 60.0)
let timeoutOverride = Double(ProcessInfo.processInfo.environment["SMARTSSD_TIMEOUT_SECONDS"] ?? "")

let dryRun = CommandLine.arguments.contains("--dry-run")
let checkOnly = CommandLine.arguments.contains("--check")
let unmountNow = CommandLine.arguments.contains("--unmount-now")
let forceRequested = CommandLine.arguments.contains("--force")

let logFormatter: DateFormatter = {
    let f = DateFormatter()
    f.dateFormat = "yyyy-MM-dd HH:mm:ss"
    return f
}()

func log(_ msg: String) {
    print("[\(logFormatter.string(from: Date()))] \(msg)")
    fflush(stdout)
}

// MARK: - Config

struct Config {
    var volumes: [String] = []
    var timeoutMinutes: Int = 10
    var cooldownMinutes: Int = 10
    var forceUnmount: Bool = false
    var unmountOnSleep: Bool = true

    static func load(from path: String) -> Config {
        var cfg = Config()
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: path)),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            logOnce("config", "⚠️  Could not read config at \(path); using defaults.")
            return cfg
        }
        cfg.volumes = (json["volumes"] as? [String]) ?? []
        cfg.timeoutMinutes = max(1, min(120, json["timeoutMinutes"] as? Int ?? cfg.timeoutMinutes))
        cfg.cooldownMinutes = max(1, min(1440, json["cooldownMinutes"] as? Int ?? cfg.cooldownMinutes))
        cfg.forceUnmount = json["forceUnmount"] as? Bool ?? cfg.forceUnmount
        cfg.unmountOnSleep = json["unmountOnSleep"] as? Bool ?? cfg.unmountOnSleep
        return cfg
    }
}

var loggedOnceKeys = Set<String>()

func logOnce(_ key: String, _ msg: String) {
    if loggedOnceKeys.insert(key).inserted {
        log(msg)
    }
}

// MARK: - Shell

@discardableResult
func run(_ cmd: String, _ args: [String], timeout: TimeInterval? = nil) -> (Int32, String) {
    let task = Process()
    let pipe = Pipe()
    task.standardOutput = pipe
    task.standardError = pipe
    task.launchPath = cmd
    task.arguments = args

    // Read the pipe concurrently: otherwise a command with >64 KB of output
    // (e.g. lsof) blocks on write and never exits.
    let buffer = NSMutableData()
    let readGroup = DispatchGroup()
    readGroup.enter()
    DispatchQueue.global(qos: .utility).async {
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        buffer.append(data)
        readGroup.leave()
    }

    do {
        try task.run()
    } catch {
        pipe.fileHandleForWriting.closeFile()
        readGroup.wait()
        return (-1, "failed to launch \(cmd): \(error.localizedDescription)")
    }
    pipe.fileHandleForWriting.closeFile()

    if let timeout = timeout {
        let watchdog = DispatchWorkItem {
            if task.isRunning { task.terminate() }
        }
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + timeout, execute: watchdog)
        task.waitUntilExit()
        watchdog.cancel()
    } else {
        task.waitUntilExit()
    }
    readGroup.wait()

    let out = String(data: buffer as Data, encoding: .utf8) ?? ""
    return (task.terminationStatus, out)
}

// MARK: - diskutil (plist based, no fragile text parsing)

func diskInfo(_ id: String) -> [String: Any]? {
    let (status, out) = run("/usr/sbin/diskutil", ["info", "-plist", id], timeout: 15)
    guard status == 0, let data = out.data(using: .utf8) else { return nil }
    return (try? PropertyListSerialization.propertyList(from: data, options: [], format: nil)) as? [String: Any]
}

struct VolumeEntry {
    let name: String
    let id: String
    let mountPoint: String
}

func allVolumes() -> [VolumeEntry] {
    let (status, out) = run("/usr/sbin/diskutil", ["list", "-plist"], timeout: 20)
    guard status == 0,
          let data = out.data(using: .utf8),
          let plist = try? PropertyListSerialization.propertyList(from: data, options: [], format: nil) as? [String: Any],
          let disks = plist["AllDisksAndPartitions"] as? [[String: Any]] else {
        logOnce("diskutil-list", "⚠️  Could not read diskutil list -plist.")
        return []
    }

    var result: [VolumeEntry] = []

    func append(_ dict: [String: Any]) {
        guard let name = dict["VolumeName"] as? String, !name.isEmpty,
              let id = dict["DeviceIdentifier"] as? String, !id.isEmpty else { return }
        result.append(VolumeEntry(
            name: name,
            id: id,
            mountPoint: dict["MountPoint"] as? String ?? ""
        ))
    }

    for disk in disks {
        append(disk)
        if let parts = disk["Partitions"] as? [[String: Any]] {
            for p in parts { append(p) }
        }
        if let vols = disk["APFSVolumes"] as? [[String: Any]] {
            for v in vols { append(v) }
        }
    }
    return result
}

func matchVolume(_ requested: String, in volumes: [VolumeEntry]) -> VolumeEntry? {
    let wanted = requested.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !wanted.isEmpty else { return nil }

    if let exact = volumes.first(where: { $0.name == wanted }) { return exact }
    if let ci = volumes.first(where: { $0.name.lowercased() == wanted.lowercased() }) { return ci }

    // Tolerate a truncated/typo'd name ("Без названи" vs "Без названия"),
    // but only when the match is unambiguous.
    let lower = wanted.lowercased()
    let candidates = volumes.filter {
        let n = $0.name.lowercased()
        return n.hasPrefix(lower) || lower.hasPrefix(n)
    }
    if candidates.count == 1 {
        logOnce("partial:\(wanted):\(candidates[0].name)",
                "⚠️  Volume '\(wanted)' matched partially as '\(candidates[0].name)'.")
        return candidates[0]
    }
    return nil
}

struct DiskTarget {
    let physicalID: String
    let info: [String: Any]
}

/// Resolve an APFS volume / partition slice to its real whole physical disk.
/// Example: disk5s1 -> APFS container disk5 -> physical store disk4s1 -> disk4.
func resolvePhysicalDisk(_ volumeInfo: [String: Any]) -> DiskTarget? {
    var currentInfo = volumeInfo
    var currentID = volumeInfo["DeviceIdentifier"] as? String ?? ""
    var visited = Set<String>()

    for _ in 0..<10 {
        if currentID.isEmpty || visited.contains(currentID) { return nil }
        visited.insert(currentID)

        // APFS volume or container: follow the physical store, skip the
        // synthesized container entirely.
        if let stores = currentInfo["APFSPhysicalStores"] as? [[String: Any]],
           let storeID = stores.first?["APFSPhysicalStore"] as? String,
           let storeInfo = diskInfo(storeID) {
            if let parent = storeInfo["ParentWholeDisk"] as? String,
               let parentInfo = diskInfo(parent),
               (parentInfo["WholeDisk"] as? Bool) == true {
                return DiskTarget(physicalID: parent, info: parentInfo)
            }
            if (storeInfo["WholeDisk"] as? Bool) == true {
                return DiskTarget(physicalID: storeID, info: storeInfo)
            }
            currentInfo = storeInfo
            currentID = storeID
            continue
        }

        if let parent = currentInfo["ParentWholeDisk"] as? String, parent != currentID,
           let parentInfo = diskInfo(parent) {
            if (parentInfo["WholeDisk"] as? Bool) == true {
                return DiskTarget(physicalID: parent, info: parentInfo)
            }
            currentInfo = parentInfo
            currentID = parent
        } else {
            if (currentInfo["WholeDisk"] as? Bool) == true {
                return DiskTarget(physicalID: currentID, info: currentInfo)
            }
            return nil
        }
    }
    return nil
}

var cachedBootDiskID: String?? = nil

/// Physical disk that hosts the running system. Never touch it.
func bootPhysicalDiskID() -> String? {
    if let cached = cachedBootDiskID { return cached }
    let id = diskInfo("/").flatMap { resolvePhysicalDisk($0)?.physicalID }
    cachedBootDiskID = .some(id)
    return id
}

/// nil = safe to unmount; otherwise the reason why we must not touch it.
func safetyIssue(_ info: [String: Any]) -> String? {
    guard (info["WholeDisk"] as? Bool) == true else { return "not a whole disk" }
    if (info["Internal"] as? Bool) == true {
        return "internal disk — refusing to touch it"
    }
    if let id = info["DeviceIdentifier"] as? String,
       let boot = bootPhysicalDiskID(), id == boot {
        return "boot disk — refusing to touch it"
    }
    return nil
}

struct MountedTarget {
    let volume: VolumeEntry
    let target: DiskTarget
}

func mountedTargets(for names: [String], verbose: Bool = true) -> [MountedTarget] {
    let volumes = allVolumes()
    var result: [MountedTarget] = []
    var seen = Set<String>()

    for name in names {
        guard let entry = matchVolume(name, in: volumes) else {
            if verbose { logOnce("missing:\(name)", "⚠️  Volume '\(name)' not found.") }
            continue
        }
        guard !entry.mountPoint.isEmpty, fm.fileExists(atPath: entry.mountPoint) else { continue }
        guard !seen.contains(entry.id) else { continue }
        seen.insert(entry.id)

        guard let info = diskInfo(entry.id) else {
            logOnce("info:\(entry.id)", "⚠️  Could not read disk info for '\(entry.name)' (\(entry.id)).")
            continue
        }
        guard let target = resolvePhysicalDisk(info) else {
            logOnce("resolve:\(entry.id)", "⚠️  Could not resolve the physical disk for '\(entry.name)'.")
            continue
        }
        result.append(MountedTarget(volume: entry, target: target))
    }
    return result
}

// MARK: - Activity detection

/// Returns paths of open files under the mount points.
/// nil means "could not determine" — callers must treat that as busy.
func openFileHits(mountPoints: [String]) -> [String]? {
    guard !mountPoints.isEmpty else { return [] }
    let (status, out) = run("/usr/sbin/lsof", ["-w", "-n", "-P", "-F", "pcn"], timeout: 20)
    guard status == 0 else { return nil }

    var hits: [String] = []
    for line in out.split(separator: "\n") {
        guard line.hasPrefix("n") else { continue }
        let path = String(line.dropFirst())
        for mp in mountPoints {
            let base = mp.hasSuffix("/") ? mp : mp + "/"
            if path == mp || path.hasPrefix(base) {
                hits.append(path)
                break
            }
        }
    }
    return hits
}

// MARK: - State

var lastBusy: [String: Date] = [:]       // physical disk -> last activity / grace
var lastAttempt: [String: Date] = [:]    // physical disk -> last unmount attempt
var coolingSince: [String: Date] = [:]   // physical disk -> when we auto-unmounted it
let rearmAfterSeconds: TimeInterval = 120

// MARK: - Unmount

func attemptUnmount(disk: String, mounts: [MountedTarget], cfg: Config, reason: String, forceOverride: Bool? = nil) {
    let force = forceOverride ?? cfg.forceUnmount
    let names = mounts.map { $0.volume.name }.joined(separator: ", ")
    log("\(reason) Unmounting \(disk) (volumes: \(names))\(force ? " [FORCE]" : "").")
    lastAttempt[disk] = Date()

    if dryRun {
        log("DRY RUN: /usr/sbin/diskutil unmountDisk\(force ? " force" : "") \(disk)")
        return
    }

    var args = ["unmountDisk"]
    if force { args.append("force") }
    args.append(disk)

    let (status, out) = run("/usr/sbin/diskutil", args, timeout: 120)
    let trimmed = out.trimmingCharacters(in: .whitespacesAndNewlines)
    if status == 0 {
        log("✅ Unmounted \(disk).")
        coolingSince[disk] = Date()
        lastBusy.removeValue(forKey: disk)
    } else {
        log("❌ Failed to unmount \(disk) (exit \(status)).")
        if !trimmed.isEmpty { log("   diskutil: \(trimmed)") }
    }
}

// MARK: - Idle logic

func checkIdle(cfg: Config) {
    let now = Date()
    let targets = mountedTargets(for: cfg.volumes)
    let grouped = Dictionary(grouping: targets, by: { $0.target.physicalID })

    for disk in grouped.keys.sorted() {
        let mounts = grouped[disk]!
        let info = mounts[0].target.info

        if let reason = safetyIssue(info) {
            logOnce("unsafe:\(disk)", "Refusing to manage \(disk): \(reason).")
            continue
        }

        let mountPoints = mounts.map { $0.volume.mountPoint }
        guard let hits = openFileHits(mountPoints: mountPoints) else {
            logOnce("lsof:\(disk)", "Could not check open files on \(disk); skipping this cycle.")
            lastBusy[disk] = now
            continue
        }

        if !hits.isEmpty {
            lastBusy[disk] = now
            if coolingSince[disk] != nil {
                log("Activity detected on \(disk); re-arming the idle timer.")
                coolingSince.removeValue(forKey: disk)
            }
            continue
        }

        // We unmounted this disk automatically. If it came back almost
        // immediately, assume the system remounted it and do not fight;
        // if it was remounted much later, treat it as a user action.
        if let unmountedAt = coolingSince[disk] {
            if now.timeIntervalSince(unmountedAt) > rearmAfterSeconds {
                log("\(disk) was remounted after the automatic unmount; re-arming.")
                coolingSince.removeValue(forKey: disk)
                lastBusy[disk] = now
            } else {
                continue
            }
        }

        if lastBusy[disk] == nil {
            lastBusy[disk] = now
            log("Watching \(disk) (volumes: \(mounts.map { $0.volume.name }.joined(separator: ", "))).")
            continue
        }

        let idleSeconds = now.timeIntervalSince(lastBusy[disk]!)
        let timeoutSeconds = timeoutOverride ?? (Double(cfg.timeoutMinutes) * 60)
        if idleSeconds < timeoutSeconds { continue }

        if let attempted = lastAttempt[disk],
           now.timeIntervalSince(attempted) < Double(cfg.cooldownMinutes) * 60 {
            continue
        }

        attemptUnmount(disk: disk, mounts: mounts, cfg: cfg, reason: "Idle timeout reached.")
    }

    // Forget per-disk state when the disk is no longer mounted/connected.
    // Keep coolingSince: it is needed when the disk comes back.
    let active = Set(grouped.keys)
    for key in Array(lastBusy.keys) where !active.contains(key) {
        lastBusy.removeValue(forKey: key)
    }
    for key in Array(lastAttempt.keys) where !active.contains(key) {
        lastAttempt.removeValue(forKey: key)
    }
    for (key, date) in Array(coolingSince) where now.timeIntervalSince(date) > 12 * 3600 {
        coolingSince.removeValue(forKey: key)
    }
}

// MARK: - Sleep watcher

class SleepWatcher: NSObject {
    @objc func willSleep(_ notification: Notification) {
        let cfg = Config.load(from: configPath)
        guard cfg.unmountOnSleep else { return }

        log("Sleep detected; unmounting configured volumes (non-destructive mode).")
        let targets = mountedTargets(for: cfg.volumes)
        let grouped = Dictionary(grouping: targets, by: { $0.target.physicalID })

        for disk in grouped.keys.sorted() {
            let mounts = grouped[disk]!
            if let reason = safetyIssue(mounts[0].target.info) {
                log("Refusing to unmount \(disk): \(reason).")
                continue
            }
            attemptUnmount(disk: disk, mounts: mounts, cfg: cfg, reason: "Sleep:")
        }
    }
}

// MARK: - CLI modes

func printStatus() {
    let cfg = Config.load(from: configPath)
    log("Config: \(configPath)")
    log("Volumes: \(cfg.volumes.isEmpty ? "(none)" : cfg.volumes.joined(separator: ", "))")
    log("Timeout: \(cfg.timeoutMinutes) min, cooldown: \(cfg.cooldownMinutes) min, force: \(cfg.forceUnmount), unmountOnSleep: \(cfg.unmountOnSleep)")

    let volumes = allVolumes()
    for name in cfg.volumes {
        guard let entry = matchVolume(name, in: volumes) else {
            log("• \(name): NOT FOUND")
            continue
        }
        let mounted = !entry.mountPoint.isEmpty && fm.fileExists(atPath: entry.mountPoint)
        log("• \(name): \(mounted ? "mounted at \(entry.mountPoint)" : "not mounted") [\(entry.id)]")

        guard let info = diskInfo(entry.id), let target = resolvePhysicalDisk(info) else {
            log("  physical disk: unresolved")
            continue
        }
        let safety = safetyIssue(target.info) ?? "OK (not internal, not boot disk)"
        log("  physical disk: \(target.physicalID) — \(safety)")

        if mounted {
            if let hits = openFileHits(mountPoints: [entry.mountPoint]) {
                log("  open files under volume: \(hits.count)")
            } else {
                log("  open files under volume: unknown (lsof failed)")
            }
        }
    }
}

func manualUnmount() {
    let cfg = Config.load(from: configPath)
    let targets = mountedTargets(for: cfg.volumes)
    let grouped = Dictionary(grouping: targets, by: { $0.target.physicalID })
    if grouped.isEmpty {
        log("No mounted configured volumes found.")
        return
    }
    for disk in grouped.keys.sorted() {
        let mounts = grouped[disk]!
        if let reason = safetyIssue(mounts[0].target.info) {
            log("Refusing to unmount \(disk): \(reason).")
            continue
        }
        attemptUnmount(disk: disk, mounts: mounts, cfg: cfg, reason: "Manual:", forceOverride: forceRequested ? true : nil)
    }
}

// MARK: - Entry point

if checkOnly {
    printStatus()
    exit(0)
}

if unmountNow {
    manualUnmount()
    exit(0)
}

log("SmartSSDManager started. Config: \(configPath)\(dryRun ? " [DRY RUN]" : "")")

let watcher = SleepWatcher()
NSWorkspace.shared.notificationCenter.addObserver(
    watcher,
    selector: #selector(SleepWatcher.willSleep(_:)),
    name: NSWorkspace.willSleepNotification,
    object: nil
)

// First evaluation shortly after launch, then once per tick.
DispatchQueue.main.asyncAfter(deadline: .now() + 5) {
    checkIdle(cfg: Config.load(from: configPath))
}
Timer.scheduledTimer(withTimeInterval: tickInterval, repeats: true) { _ in
    checkIdle(cfg: Config.load(from: configPath))
}
RunLoop.main.run()
