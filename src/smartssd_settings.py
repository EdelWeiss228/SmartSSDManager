#!/usr/bin/env python3
"""SmartSSD Settings — opens in browser via local HTTP server."""

import html
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse
from xml.sax.saxutils import escape as xml_escape

HOME        = os.path.expanduser("~")
BIN         = os.path.join(HOME, ".local", "bin")
CONFIG_PATH = os.path.join(BIN, "smartssd_config.json")
PLIST_PATH  = os.path.join(HOME, "Library", "LaunchAgents", "com.user.smartssd.plist")
ICON_PATH   = "/System/Library/Extensions/IOStorageFamily.kext/Contents/Resources/External.icns"
DESKTOP     = os.path.join(HOME, "Desktop")
PORT        = 19432
LABEL       = "com.user.smartssd"

LSREGISTER  = ("/System/Library/Frameworks/CoreServices.framework"
               "/Frameworks/LaunchServices.framework/Support/lsregister")
DISKUTIL    = "/usr/sbin/diskutil"

DEFAULTS = {
    "volumes": [],
    "timeoutMinutes": 10,
    "cooldownMinutes": 10,
    "forceUnmount": False,
    "unmountOnSleep": True,
}

# ── Config ──────────────────────────────────────────────────────────────────────
def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg.update(data)
    except Exception:
        pass

    cfg["volumes"] = [str(v).strip() for v in cfg.get("volumes", []) if str(v).strip()]
    try:
        cfg["timeoutMinutes"] = max(1, min(120, int(cfg.get("timeoutMinutes", 10))))
    except Exception:
        cfg["timeoutMinutes"] = 10
    try:
        cfg["cooldownMinutes"] = max(1, min(1440, int(cfg.get("cooldownMinutes", 10))))
    except Exception:
        cfg["cooldownMinutes"] = 10
    cfg["forceUnmount"] = bool(cfg.get("forceUnmount", False))
    cfg["unmountOnSleep"] = bool(cfg.get("unmountOnSleep", True))
    return cfg

def save_config(cfg):
    os.makedirs(BIN, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG_PATH)

def _diskutil_plist(*args, timeout=10):
    try:
        result = subprocess.run([DISKUTIL, *args], capture_output=True,
                                timeout=timeout)
        if result.returncode != 0:
            return {}
        return plistlib.loads(result.stdout)
    except Exception:
        return {}

def _disk_info(identifier):
    return _diskutil_plist("info", "-plist", identifier)

@lru_cache(maxsize=1)
def _boot_physical_disk_ids():
    """Physical whole disks that host the running system. Never offer them."""
    info = _disk_info("/")
    ids = set()

    stores = info.get("APFSPhysicalStores") or []
    for store in stores:
        if not isinstance(store, dict):
            continue
        store_id = store.get("APFSPhysicalStore") or store.get("DeviceIdentifier")
        if not store_id:
            continue
        store_info = _disk_info(store_id)
        parent = store_info.get("ParentWholeDisk") or store_id
        if parent:
            ids.add(parent)

    parent = info.get("ParentWholeDisk")
    if parent:
        parent_info = _disk_info(parent)
        if (parent_info.get("WholeDisk") and
                parent_info.get("VirtualOrPhysical") not in ("Virtual", "Synthesized")):
            ids.add(parent)
        for store in parent_info.get("APFSPhysicalStores") or []:
            if not isinstance(store, dict):
                continue
            store_id = store.get("APFSPhysicalStore") or store.get("DeviceIdentifier")
            if store_id:
                store_info = _disk_info(store_id)
                store_parent = store_info.get("ParentWholeDisk") or store_id
                if store_parent:
                    ids.add(store_parent)

    return ids

def list_external_volumes():
    """Return volumes on connected external physical disks.

    Internal disks, disk images, synthesized APFS containers and the boot
    disk are intentionally excluded. Unmounted volumes are included so they
    can be selected before the daemon mounts/unmounts them.
    """
    data = _diskutil_plist("list", "-plist")
    entries = data.get("AllDisksAndPartitions") or []
    if not isinstance(entries, list):
        return []

    boot_ids = _boot_physical_disk_ids()
    whole_entries = {}
    external = set()

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        whole_id = entry.get("DeviceIdentifier")
        if not whole_id:
            continue
        whole_entries[whole_id] = entry

        info = _disk_info(whole_id)
        if not info.get("WholeDisk"):
            continue
        if info.get("Internal") is True:
            continue
        if info.get("VirtualOrPhysical") in ("Virtual", "Synthesized"):
            continue
        if info.get("BusProtocol") == "Disk Image":
            continue
        if not (info.get("Ejectable") or
                info.get("RemovableMediaOrExternalDevice")):
            continue
        if whole_id in boot_ids:
            continue
        external.add(whole_id)

    # Map every partition slice back to its external whole disk. This is
    # needed to resolve APFS volumes, which live on a synthesized container.
    partition_to_whole = {}
    for whole_id in external:
        for part in whole_entries[whole_id].get("Partitions") or []:
            part_id = part.get("DeviceIdentifier")
            if part_id:
                partition_to_whole[part_id] = whole_id

    volumes = []
    seen_names = set()

    def add_volume(name, device, mount_point, size, whole_id):
        name = str(name or "").strip()
        if not name or not is_valid_volume_name(name):
            return
        if name in seen_names:
            return
        if whole_id in boot_ids:
            return
        mount_point = str(mount_point or "")
        if mount_point == "/" or mount_point.startswith("/System/Volumes/"):
            return
        seen_names.add(name)
        volumes.append({
            "name": name,
            "device": str(device or ""),
            "mount_point": mount_point,
            "mounted": bool(mount_point),
            "size": size if isinstance(size, int) else 0,
            "whole_disk": whole_id,
        })

    # Ordinary partitions: ExFAT, NTFS, HFS+, FAT, etc.
    for whole_id in external:
        for part in whole_entries[whole_id].get("Partitions") or []:
            add_volume(part.get("VolumeName"), part.get("DeviceIdentifier"),
                       part.get("MountPoint"), part.get("Size"), whole_id)

    # APFS volumes: physical disk -> partition store -> synthesized container.
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        apfs_volumes = entry.get("APFSVolumes") or []
        if not apfs_volumes:
            continue
        stores = []
        for store in entry.get("APFSPhysicalStores") or []:
            if isinstance(store, dict):
                store_id = store.get("DeviceIdentifier") or store.get("APFSPhysicalStore")
                if store_id:
                    stores.append(store_id)
        if not stores:
            continue
        wholes = [partition_to_whole.get(store_id) for store_id in stores]
        if any(whole_id not in external for whole_id in wholes):
            continue
        whole_id = wholes[0]
        for volume in apfs_volumes:
            add_volume(volume.get("VolumeName"), volume.get("DeviceIdentifier"),
                       volume.get("MountPoint"), volume.get("Size"), whole_id)

    volumes.sort(key=lambda volume: (not volume["mounted"],
                                     volume["name"].lower()))
    return volumes

def format_size(size):
    try:
        value = float(size)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    units = ("Б", "КБ", "МБ", "ГБ", "ТБ", "ПБ")
    index = 0
    while value >= 1000 and index < len(units) - 1:
        value /= 1000
        index += 1
    if index == 0:
        return f"{int(value)} {units[index]}"
    return f"{value:.1f} {units[index]}"

def is_valid_volume_name(name: str) -> bool:
    return (bool(name) and "/" not in name and ":" not in name
            and name not in (".", "..") and not any(ord(c) < 32 for c in name))

# ── Shortcuts ───────────────────────────────────────────────────────────────────
LAUNCHER_SH = """\
#!/bin/bash
VOL={volq}
DISK_ID=$(diskutil list | grep -F -- "$VOL" | awk '{{print $NF}}' | head -n 1)
if [ -z "$DISK_ID" ]; then
    osascript -e 'display alert "Диск не найден" message "Убедитесь, что SSD подключён к Mac." as warning'
    exit 1
fi
diskutil mount "$DISK_ID" 2>/dev/null
for i in 1 2 3 4 5; do
    [ -d "/Volumes/$VOL" ] && break
    sleep 1
done
if [ -d "/Volumes/$VOL" ]; then
    open "/Volumes/$VOL"
else
    osascript -e 'display alert "Не удалось смонтировать" message "Попробуйте переподключить SSD." as warning'
fi
"""

INFO_PLIST = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
    <key>CFBundleExecutable</key><string>launcher</string>
    <key>CFBundleIconFile</key><string>SSDDisk</string>
    <key>CFBundleName</key><string>{label}</string>
    <key>CFBundleIdentifier</key><string>com.user.ssd-{safe}</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>LSUIElement</key><true/>
</dict></plist>
"""

def create_shortcut(vol: str):
    safe  = re.sub(r"[^a-z0-9]+", "-", vol.lower()).strip("-") or "disk"
    app   = os.path.join(DESKTOP, f"{vol}.app")
    macos = os.path.join(app, "Contents", "MacOS")
    res   = os.path.join(app, "Contents", "Resources")
    os.makedirs(macos, exist_ok=True)
    os.makedirs(res,   exist_ok=True)
    if os.path.exists(ICON_PATH):
        shutil.copy(ICON_PATH, os.path.join(res, "SSDDisk.icns"))
    with open(os.path.join(app, "Contents", "Info.plist"), "w") as f:
        f.write(INFO_PLIST.format(label=xml_escape(vol), safe=safe))
    launcher = os.path.join(macos, "launcher")
    with open(launcher, "w") as f:
        f.write(LAUNCHER_SH.format(volq=shlex.quote(vol)))
    os.chmod(launcher, 0o755)
    try:
        subprocess.run([LSREGISTER, "-f", app], capture_output=True, timeout=10)
    except Exception:
        pass

def remove_shortcut(vol: str):
    app = os.path.join(DESKTOP, f"{vol}.app")
    if os.path.exists(app):
        try:
            subprocess.run([LSREGISTER, "-u", app], capture_output=True, timeout=10)
        except Exception:
            pass
        shutil.rmtree(app)

def _launchctl(args, timeout=10):
    try:
        return subprocess.run(["/bin/launchctl"] + args,
                              capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None

def _service_loaded(service):
    result = _launchctl(["print", service], timeout=5)
    return result is not None and result.returncode == 0

def restart_daemon():
    disabled = PLIST_PATH + ".disabled"
    if not os.path.exists(PLIST_PATH) and os.path.exists(disabled):
        os.replace(disabled, PLIST_PATH)
    if not os.path.exists(PLIST_PATH):
        return False, "LaunchAgent не найден. Запустите install.sh."

    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{LABEL}"

    # Обычный случай: сервис уже загружен. kickstart -k перезапускает его
    # без bootout/bootstrap и без гонки, из-за которой сервис мог исчезнуть.
    if _service_loaded(service):
        result = _launchctl(["kickstart", "-k", service])
        if result is not None and result.returncode == 0:
            time.sleep(0.3)
            if _service_loaded(service):
                return True, "Демон перезапущен с новыми настройками"

    # Сервис не загружен (или kickstart не сработал): полный bootstrap.
    _launchctl(["bootout", service])
    deadline = time.time() + 5
    while time.time() < deadline and _service_loaded(service):
        time.sleep(0.2)

    _launchctl(["enable", service])
    last_error = "launchctl не ответил"
    for _ in range(2):
        result = _launchctl(["bootstrap", domain, PLIST_PATH])
        if result is not None and result.returncode == 0:
            time.sleep(0.2)
            if _service_loaded(service):
                return True, "Демон перезапущен с новыми настройками"
            last_error = "bootstrap вернул успех, но сервис не загрузился"
        elif result is not None:
            last_error = (result.stderr or result.stdout or
                          "launchctl bootstrap error").strip()
        time.sleep(0.5)

    return False, f"Не удалось запустить демон: {last_error}"

# ── HTML ────────────────────────────────────────────────────────────────────────
def render_html(msg="", error=False):
    cfg = load_config()
    vols = cfg["volumes"]
    t = cfg["timeoutMinutes"]
    external = list_external_volumes()
    external_by_name = {info["name"]: info for info in external}
    configured = set(vols)

    # Volumes already tracked by the daemon.
    rows = ""
    for vol in vols:
        info = external_by_name.get(vol)
        if info is None:
            badge_cls, badge_text = "missing", "не найден"
            meta = '<div class="vol-meta">нет среди подключённых внешних томов</div>'
        else:
            if info["mounted"]:
                badge_cls, badge_text = "mounted", "смонтирован"
            else:
                badge_cls, badge_text = "unmounted", "не смонтирован"
            meta_bits = [html.escape(info["device"])]
            if info["mount_point"]:
                meta_bits.append(html.escape(info["mount_point"]))
            size = format_size(info["size"])
            if size:
                meta_bits.append(html.escape(size))
            meta = '<div class="vol-meta">' + " · ".join(meta_bits) + "</div>"
        rows += f"""<tr>
              <td><span class="vol-name">{html.escape(vol)}</span>{meta}</td>
              <td><span class="badge {badge_cls}">{badge_text}</span></td>
              <td><a class="del-btn" href="/delete?vol={quote(vol)}">✕</a></td>
            </tr>"""
    if not rows:
        rows = '<tr><td colspan="3" style="color:#6c7086;text-align:center;">Список пуст</td></tr>'

    # Volumes discovered on connected external physical disks.
    ext_rows = ""
    available = 0
    for index, info in enumerate(external):
        name = info["name"]
        added = name in configured
        if not added:
            available += 1
        if info["mounted"]:
            badge_cls, badge_text = "mounted", "смонтирован"
        else:
            badge_cls, badge_text = "unmounted", "не смонтирован"
        meta_bits = [html.escape(info["device"])]
        if info["mount_point"]:
            meta_bits.append(html.escape(info["mount_point"]))
        size = format_size(info["size"])
        if size:
            meta_bits.append(html.escape(size))
        meta = '<div class="vol-meta">' + " · ".join(meta_bits) + "</div>"

        if added:
            check = '<span class="badge added">✓</span>'
            name_html = f'<span class="vol-name">{html.escape(name)}</span>'
            extra = '<div class="vol-meta">уже в списке</div>'
        else:
            check = (f'<input type="checkbox" class="vol-check" name="vol" '
                     f'value="{html.escape(name)}" id="ext-{index}">')
            name_html = (f'<label for="ext-{index}" class="vol-name">'
                         f'{html.escape(name)}</label>')
            extra = ""
        ext_rows += f"""<tr>
              <td class="check-cell">{check}</td>
              <td>{name_html}{meta}</td>
              <td><span class="badge {badge_cls}">{badge_text}</span>{extra}</td>
            </tr>"""

    empty_hint = ""
    if external and not available:
        empty_hint = '<p class="hint">Все подключённые внешние тома уже добавлены.</p>'
    if external:
        ext_block = f"""<form action="/add" method="get">
      <table class="vol-table">
        <thead><tr><th></th><th>Том</th><th>Статус</th></tr></thead>
        <tbody>{ext_rows}</tbody>
      </table>
      <div class="add-actions">
        <button type="submit" {'disabled' if not available else ''}>＋ Добавить выбранные</button>
        <a class="refresh" href="/">↻ Обновить список</a>
      </div>
      {empty_hint}
    </form>"""
    else:
        ext_block = ('<p class="hint">Внешние тома не найдены. Подключите SSD и '
                     '<a class="refresh" href="/">обновите список</a>.</p>')

    status_html = ""
    if msg:
        cls = "err" if error else "ok"
        status_html = f'<div class="status {cls}">{html.escape(msg)}</div>'

    force_checked = "checked" if cfg["forceUnmount"] else ""
    sleep_checked = "checked" if cfg["unmountOnSleep"] else ""

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<title>SmartSSD Settings</title>
<style>
  :root {{
    --bg: #1e1e2e; --surface: #2a2a3e; --border: #45475a;
    --text: #cdd6f4; --muted: #6c7086;
    --accent: #cba6f7; --red: #f38ba8; --green: #a6e3a1;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, "SF Pro Display", sans-serif;
    background: var(--bg); color: var(--text);
    display: flex; justify-content: center; align-items: flex-start;
    min-height: 100vh; padding: 40px 20px;
  }}
  .card {{
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 16px; padding: 32px; width: 560px;
    box-shadow: 0 20px 60px rgba(0,0,0,.5);
  }}
  h1 {{ font-size: 22px; font-weight: 600; margin-bottom: 6px; }}
  .subtitle {{ color: var(--muted); font-size: 13px; margin-bottom: 28px; }}
  section {{ margin-bottom: 28px; }}
  h2 {{ font-size: 12px; text-transform: uppercase; letter-spacing: .08em;
        color: var(--muted); margin-bottom: 12px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  td {{ padding: 10px 12px; border-bottom: 1px solid var(--border); font-size: 14px; }}
  tr:last-child td {{ border-bottom: none; }}
  .vol-table th {{ color: var(--muted); font-size: 11px; font-weight: 600;
                   text-align: left; padding: 0 12px 8px;
                   border-bottom: 1px solid var(--border); }}
  .vol-table td {{ vertical-align: middle; }}
  .check-cell {{ width: 28px; padding-left: 0; }}
  .vol-check {{ width: 16px; height: 16px; accent-color: var(--accent);
                cursor: pointer; }}
  .vol-meta {{ color: var(--muted); font-size: 11px; margin-top: 3px;
               line-height: 1.3; }}
  .del-btn {{
    color: var(--red); text-decoration: none; font-size: 16px;
    font-weight: 700; float: right; line-height: 1;
    padding: 2px 6px; border-radius: 6px;
    transition: background .15s;
  }}
  .del-btn:hover {{ background: rgba(243,139,168,.15); }}
  input[type=number] {{
    background: var(--bg); border: 1px solid var(--border);
    border-radius: 8px; color: var(--text); font-size: 14px;
    padding: 9px 12px; outline: none; transition: border .2s;
  }}
  input[type=number]:focus {{ border-color: var(--accent); }}
  input[type=number] {{ width: 72px; }}
  .timer-row {{ display: flex; align-items: center; gap: 12px; }}
  .timer-label {{ color: var(--muted); font-size: 13px; }}
  button, .btn {{
    background: var(--accent); color: #1e1e2e;
    border: none; border-radius: 8px; font-size: 14px;
    font-weight: 600; padding: 9px 18px; cursor: pointer;
    text-decoration: none; transition: opacity .15s;
    white-space: nowrap;
  }}
  button:hover, .btn:hover {{ opacity: .85; }}
  button:disabled {{ opacity: .45; cursor: not-allowed; }}
  .add-actions {{ display: flex; align-items: center; gap: 12px; margin-top: 12px; }}
  .add-actions button {{ padding: 8px 14px; font-size: 13px; }}
  .refresh {{ color: var(--muted); font-size: 12px; text-decoration: none; }}
  .refresh:hover {{ color: var(--accent); }}
  .btn-save {{
    width: 100%; padding: 12px; font-size: 15px;
    border-radius: 10px; margin-top: 4px;
  }}
  .status {{ border-radius: 8px; padding: 10px 14px; font-size: 13px; margin-top: 16px; }}
  .status.ok {{ background: rgba(166,227,161,.12); border: 1px solid var(--green); color: var(--green); }}
  .status.err {{ background: rgba(243,139,168,.12); border: 1px solid var(--red); color: var(--red); }}
  .vol-name {{ font-family: "SF Mono", monospace; font-size: 13px; }}
  label.vol-name {{ cursor: pointer; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 999px;
            font-size: 11px; white-space: nowrap; }}
  .badge.mounted {{ color: var(--green); background: rgba(166,227,161,.12); }}
  .badge.unmounted {{ color: #f9e2af; background: rgba(249,226,175,.12); }}
  .badge.missing {{ color: var(--red); background: rgba(243,139,168,.12); }}
  .badge.added {{ color: var(--green); background: rgba(166,227,161,.12); }}
  .check-row {{ display: flex; align-items: flex-start; gap: 10px; margin-bottom: 10px;
                font-size: 13px; color: var(--text); }}
  .check-row input {{ margin-top: 2px; }}
  .hint {{ color: var(--muted); font-size: 12px; margin-top: 8px; line-height: 1.4; }}
</style>
</head>
<body>
<div class="card">
  <h1>⚙️ SmartSSD Settings</h1>
  <p class="subtitle">Настройки фонового демона управления внешним SSD</p>

  <section>
    <h2>Подключённые внешние тома</h2>
    {ext_block}
    <p class="hint">Показываются только физические внешние диски. Внутренние диски
    и загрузочный том недоступны для выбора.</p>
  </section>

  <section>
    <h2>Отслеживаемые тома</h2>
    <table class="vol-table">
      <thead><tr><th>Том</th><th>Статус</th><th></th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
    <p class="hint">Демон извлекает только эти тома. Если том сейчас не подключён,
    он начнёт отслеживаться, когда появится.</p>
  </section>

  <section>
    <h2>Таймер простоя</h2>
    <form class="timer-row" action="/save_timer" method="get">
      <input type="number" name="t" value="{t}" min="1" max="120" required>
      <span class="timer-label">минут без активности → авто-извлечение</span>
      <button type="submit">✓</button>
    </form>
  </section>

  <section>
    <h2>Безопасность</h2>
    <form action="/save_safety" method="get">
      <label class="check-row">
        <input type="checkbox" name="force" value="1" {force_checked}>
        <span>Принудительное извлечение, даже если диск занят.<br>
        <b>Риск потери данных.</b> По умолчанию выключено.</span>
      </label>
      <label class="check-row">
        <input type="checkbox" name="sleep" value="1" {sleep_checked}>
        <span>Извлекать диски перед уходом Mac в сон</span>
      </label>
      <button type="submit">✓ Сохранить</button>
    </form>
  </section>

  <form action="/save_and_restart" method="get">
    <button class="btn-save" type="submit">💾 Сохранить и перезапустить демон</button>
  </form>

  {status_html}
</div>
</body>
</html>"""

# ── HTTP handler ────────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass   # silence access log

    def _respond(self, html_text, status=200):
        body = html_text.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, msg="", error=False):
        if msg:
            loc = "/?msg=" + quote(msg) + ("&err=1" if error else "")
        else:
            loc = "/"
        self.send_response(302)
        self.send_header("Location", loc)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        qs     = parse_qs(parsed.query)
        path   = parsed.path

        if path == "/":
            msg   = qs.get("msg", [""])[0]
            error = qs.get("err", ["0"])[0] == "1"
            self._respond(render_html(msg, error))

        elif path == "/add":
            requested = []
            for raw in qs.get("vol", []):
                vol = raw.strip()
                if vol and vol not in requested:
                    requested.append(vol)

            if not requested:
                self._redirect("Выберите хотя бы один том из списка", error=True)
                return
            for vol in requested:
                if not is_valid_volume_name(vol):
                    self._redirect("Некорректное имя тома", error=True)
                    return

            external_names = {info["name"] for info in list_external_volumes()}
            unknown = [vol for vol in requested if vol not in external_names]
            if unknown:
                self._redirect("Том не найден среди подключённых внешних: "
                               + ", ".join(f"«{vol}»" for vol in unknown),
                               error=True)
                return

            cfg = load_config()
            added = []
            already = []
            for vol in requested:
                if vol in cfg["volumes"]:
                    already.append(vol)
                else:
                    cfg["volumes"].append(vol)
                    added.append(vol)

            if not added:
                self._redirect("Эти тома уже добавлены: "
                               + ", ".join(f"«{vol}»" for vol in already),
                               error=True)
                return

            save_config(cfg)
            for vol in added:
                create_shortcut(vol)

            msg = ("Добавлены тома: "
                   + ", ".join(f"«{vol}»" for vol in added)
                   + ". Ярлыки созданы на Рабочем столе")
            if already:
                msg += ". Уже были: " + ", ".join(f"«{vol}»" for vol in already)
            self._redirect(msg)

        elif path == "/delete":
            vol = qs.get("vol", [""])[0].strip()
            if not is_valid_volume_name(vol):
                self._redirect("Некорректное имя тома", error=True)
                return
            cfg = load_config()
            cfg["volumes"] = [v for v in cfg["volumes"] if v != vol]
            save_config(cfg)
            remove_shortcut(vol)
            self._redirect(f"Удалён том «{vol}»")

        elif path == "/save_timer":
            t = qs.get("t", ["10"])[0]
            try: t = max(1, min(120, int(t)))
            except Exception: t = 10
            cfg = load_config()
            cfg["timeoutMinutes"] = t
            save_config(cfg)
            self._redirect(f"Таймер установлен: {t} мин")

        elif path == "/save_safety":
            cfg = load_config()
            cfg["forceUnmount"] = qs.get("force", [""])[0] == "1"
            cfg["unmountOnSleep"] = qs.get("sleep", [""])[0] == "1"
            save_config(cfg)
            if cfg["forceUnmount"]:
                self._redirect("Сохранено. ВНИМАНИЕ: включено принудительное извлечение.")
            else:
                self._redirect("Настройки безопасности сохранены")

        elif path == "/save_and_restart":
            ok, msg = restart_daemon()
            self._redirect(msg, error=not ok)

        elif path == "/quit":
            self._respond("<script>window.close()</script>")
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        else:
            self._respond("<h1>404</h1>", 404)

class SettingsServer(ThreadingHTTPServer):
    daemon_threads = True

# ── Entry point ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    url = f"http://127.0.0.1:{PORT}/"
    try:
        server = SettingsServer(("127.0.0.1", PORT), Handler)
    except OSError:
        # Another instance is already running — just open it.
        if not os.environ.get("SMARTSSD_NO_BROWSER"):
            webbrowser.open(url)
        sys.exit(0)

    if not os.environ.get("SMARTSSD_NO_BROWSER"):
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    print(f"SmartSSD Settings running at {url}  (close browser tab to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
