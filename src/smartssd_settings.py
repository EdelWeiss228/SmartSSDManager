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

def known_volume_names():
    """Return all volume names currently known to diskutil (mounted or not)."""
    try:
        out = subprocess.run(["/usr/sbin/diskutil", "list", "-plist"],
                             capture_output=True, timeout=15).stdout
        data = plistlib.loads(out)
        names = []

        def walk(node):
            if isinstance(node, dict):
                name = node.get("VolumeName")
                if name:
                    names.append(name)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(data.get("AllDisksAndPartitions", []))
        return names
    except Exception:
        return []

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
    cfg  = load_config()
    vols = cfg["volumes"]
    t    = cfg["timeoutMinutes"]

    rows = "".join(
        f"""<tr>
              <td><span class="vol-name">{html.escape(v)}</span></td>
              <td><a class="del-btn" href="/delete?vol={quote(v)}">✕</a></td>
            </tr>"""
        for v in vols
    )
    if not rows:
        rows = '<tr><td colspan="2" style="color:#6c7086;text-align:center;">Список пуст</td></tr>'

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
    border-radius: 16px; padding: 32px; width: 520px;
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
  .del-btn {{
    color: var(--red); text-decoration: none; font-size: 16px;
    font-weight: 700; float: right; line-height: 1;
    padding: 2px 6px; border-radius: 6px;
    transition: background .15s;
  }}
  .del-btn:hover {{ background: rgba(243,139,168,.15); }}
  .add-row {{ display: flex; gap: 8px; margin-top: 12px; }}
  input[type=text], input[type=number] {{
    background: var(--bg); border: 1px solid var(--border);
    border-radius: 8px; color: var(--text); font-size: 14px;
    padding: 9px 12px; outline: none; transition: border .2s;
  }}
  input[type=text]:focus, input[type=number]:focus {{
    border-color: var(--accent);
  }}
  input[type=text] {{ flex: 1; }}
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
  .btn-save {{
    width: 100%; padding: 12px; font-size: 15px;
    border-radius: 10px; margin-top: 4px;
  }}
  .status {{ border-radius: 8px; padding: 10px 14px; font-size: 13px; margin-top: 16px; }}
  .status.ok {{ background: rgba(166,227,161,.12); border: 1px solid var(--green); color: var(--green); }}
  .status.err {{ background: rgba(243,139,168,.12); border: 1px solid var(--red); color: var(--red); }}
  .vol-name {{ font-family: "SF Mono", monospace; font-size: 13px; }}
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
    <h2>Тома (volume names)</h2>
    <table>{rows}</table>
    <form class="add-row" action="/add" method="get">
      <input type="text" name="vol" placeholder="Имя тома из diskutil list" required>
      <button type="submit">＋ Добавить</button>
    </form>
    <p class="hint">Имя должно совпадать с названием тома. Если том сейчас не подключён,
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
            vol = qs.get("vol", [""])[0].strip()
            if not is_valid_volume_name(vol):
                self._redirect("Некорректное имя тома", error=True)
                return
            cfg = load_config()
            if vol not in cfg["volumes"]:
                known = known_volume_names()
                cfg["volumes"].append(vol)
                save_config(cfg)
                create_shortcut(vol)
                if known and not any(n.startswith(vol) or vol.startswith(n) for n in known):
                    self._redirect(f"Том «{vol}» добавлен, но сейчас не найден. "
                                   "Он начнёт отслеживаться, когда появится.")
                    return
            self._redirect(f"Добавлен том «{vol}» и создан ярлык на Рабочем столе")

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
