#!/usr/bin/env python3
"""SmartSSD Settings — opens in browser via local HTTP server."""

import json, os, subprocess, shutil, threading, webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

HOME        = os.path.expanduser("~")
BIN         = os.path.join(HOME, ".local", "bin")
CONFIG_PATH = os.path.join(BIN, "smartssd_config.json")
PLIST_PATH  = os.path.join(HOME, "Library", "LaunchAgents", "com.user.smartssd.plist")
ICON_PATH   = "/System/Library/Extensions/IOStorageFamily.kext/Contents/Resources/External.icns"
DESKTOP     = os.path.join(HOME, "Desktop")
PORT        = 19432

LSREGISTER  = ("/System/Library/Frameworks/CoreServices.framework"
               "/Frameworks/LaunchServices.framework/Support/lsregister")

# ── Config ──────────────────────────────────────────────────────────────────────
def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {"volumes": [], "timeoutMinutes": 5}

def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

# ── Shortcuts ───────────────────────────────────────────────────────────────────
LAUNCHER_SH = """\
#!/bin/bash
DISK_ID=$(diskutil list | grep '{vol}' | awk '{{print $NF}}')
if [ -z "$DISK_ID" ]; then
    osascript -e 'display alert "Диск не найден" message "Убедитесь, что SSD подключён к Mac." as warning'
    exit 1
fi
diskutil mount "$DISK_ID" 2>/dev/null
for i in 1 2 3 4 5; do
    [ -d "/Volumes/{vol}" ] && break; sleep 1
done
if [ -d "/Volumes/{vol}" ]; then
    open "/Volumes/{vol}"
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
    safe  = vol.replace(" ", "-").lower()
    app   = os.path.join(DESKTOP, f"{vol}.app")
    macos = os.path.join(app, "Contents", "MacOS")
    res   = os.path.join(app, "Contents", "Resources")
    os.makedirs(macos, exist_ok=True)
    os.makedirs(res,   exist_ok=True)
    if os.path.exists(ICON_PATH):
        shutil.copy(ICON_PATH, os.path.join(res, "SSDDisk.icns"))
    with open(os.path.join(app, "Contents", "Info.plist"), "w") as f:
        f.write(INFO_PLIST.format(label=vol, safe=safe))
    launcher = os.path.join(macos, "launcher")
    with open(launcher, "w") as f:
        f.write(LAUNCHER_SH.format(vol=vol))
    os.chmod(launcher, 0o755)
    subprocess.run([LSREGISTER, "-f", app], capture_output=True)

def remove_shortcut(vol: str):
    app = os.path.join(DESKTOP, f"{vol}.app")
    if os.path.exists(app):
        shutil.rmtree(app)

def restart_daemon():
    subprocess.run(["launchctl", "unload", PLIST_PATH], capture_output=True)
    subprocess.run(["launchctl", "load",   PLIST_PATH], capture_output=True)

# ── HTML ────────────────────────────────────────────────────────────────────────
def render_html(msg=""):
    cfg  = load_config()
    vols = cfg.get("volumes", [])
    t    = cfg.get("timeoutMinutes", 5)

    rows = "".join(
        f"""<tr>
              <td><span class="vol-name">{v}</span></td>
              <td><a class="del-btn" href="/delete?vol={v}">✕</a></td>
            </tr>"""
        for v in vols
    )
    if not rows:
        rows = '<tr><td colspan="2" style="color:#6c7086;text-align:center;">Список пуст</td></tr>'

    status_html = f'<div class="status ok">{msg}</div>' if msg else ""

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
    border-radius: 16px; padding: 32px; width: 480px;
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
  .status.ok {{
    background: rgba(166,227,161,.12); border: 1px solid var(--green);
    color: var(--green); border-radius: 8px; padding: 10px 14px;
    font-size: 13px; margin-top: 16px;
  }}
  .vol-name {{ font-family: "SF Mono", monospace; font-size: 13px; }}
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
  </section>

  <section>
    <h2>Таймер простоя</h2>
    <form class="timer-row" action="/save_timer" method="get">
      <input type="number" name="t" value="{t}" min="1" max="120" required>
      <span class="timer-label">минут без активности → авто-извлечение</span>
      <button type="submit">✓</button>
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

    def _respond(self, html, status=200):
        body = html.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, msg=""):
        loc = f"/?msg={msg}" if msg else "/"
        self.send_response(302)
        self.send_header("Location", loc)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        qs     = parse_qs(parsed.query)
        path   = parsed.path

        if path == "/":
            msg = qs.get("msg", [""])[0]
            self._respond(render_html(msg))

        elif path == "/add":
            vol = qs.get("vol", [""])[0].strip()
            if vol:
                cfg = load_config()
                if vol not in cfg["volumes"]:
                    cfg["volumes"].append(vol)
                    save_config(cfg)
                    create_shortcut(vol)
            self._redirect(f"Добавлен том «{vol}» и создан ярлык на Рабочем столе")

        elif path == "/delete":
            vol = qs.get("vol", [""])[0].strip()
            if vol:
                cfg = load_config()
                cfg["volumes"] = [v for v in cfg["volumes"] if v != vol]
                save_config(cfg)
                remove_shortcut(vol)
            self._redirect(f"Удалён том «{vol}»")

        elif path == "/save_timer":
            t = qs.get("t", ["5"])[0]
            try: t = max(1, min(120, int(t)))
            except: t = 5
            cfg = load_config()
            cfg["timeoutMinutes"] = t
            save_config(cfg)
            self._redirect(f"Таймер установлен: {t} мин")

        elif path == "/save_and_restart":
            restart_daemon()
            self._redirect("✅ Демон перезапущен с новыми настройками")

        elif path == "/quit":
            self._respond("<script>window.close()</script>")
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        else:
            self._respond("<h1>404</h1>", 404)

# ── Entry point ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    url    = f"http://127.0.0.1:{PORT}/"
    # Open browser after short delay (server needs to be up first)
    threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    print(f"SmartSSD Settings running at {url}  (close browser tab to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
