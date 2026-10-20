"""Local VLESS dashboard. Run: python dashboard_server.py"""
from __future__ import annotations
import csv, json, shutil, threading, time, socket, re
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
import vless_speedtest as tester

ROOT = Path(__file__).resolve().parent
CONFIGS = ROOT / "configs.txt"
HISTORY = ROOT / "vless_history_full.csv"
LOGS = ROOT / "vless_logs"
STATE = {"running": False, "rows": [], "done": 0, "total": 0, "started": None, "elapsed": 0, "current": "", "phase": "", "live_speed": 0, "live_bytes": 0, "error": ""}
LOCK = threading.Lock()

def migrate_history():
    """Import earlier CSV/log results once, preserving old runs."""
    old = ROOT / "vless_history.csv"
    rows = []
    if HISTORY.exists() and HISTORY.stat().st_size > 0:
        with HISTORY.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        if rows and all(r.get("ping_ms") for r in rows):
            return
    if old.exists() and not rows:
        with old.open(encoding="utf-8-sig", newline="") as f:
            for i, r in enumerate(csv.DictReader(f), 1):
                rows.append({"run": "legacy-csv", "timestamp_utc": "", "index": i,
                             "name": r.get("name", ""), "download_mbps": r.get("download_mbps", ""),
                             "ping_ms": "", "note": r.get("note", "")})
    for i, log in enumerate(sorted(LOGS.glob("*.log")), 1):
        text = log.read_text(encoding="utf-8", errors="replace")
        name = re.search(r"^Name:\s*(.+)$", text, re.M)
        rtt = re.search(r"^RTT:\s*(\d+)\s*ms", text, re.M)
        out = re.search(r"Curl output:\s*200,([^,\r\n]+),([^\r\n]+)", text)
        mbps = ""
        if out:
            try: mbps = f"{float(out.group(2)) * 8 / 1_000_000:.2f}"
            except ValueError: pass
        matched = next((x for x in rows if x.get("name") == name.group(1) and not x.get("ping_ms")), None) if name else None
        if matched:
            if rtt: matched["ping_ms"] = rtt.group(1)
            if not matched.get("download_mbps"): matched["download_mbps"] = mbps
        elif name:
            rows.append({"run": "legacy-log", "timestamp_utc": datetime.fromtimestamp(log.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"),
                         "index": i, "name": name.group(1), "download_mbps": mbps,
                         "ping_ms": rtt.group(1) if rtt else "", "note": "imported from log"})
    if rows:
        with HISTORY.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["run", "timestamp_utc", "index", "name", "download_mbps", "ping_ms", "note"])
            w.writeheader(); w.writerows(rows)

def set_error(message):
    with LOCK:
        STATE.update(running=False, current="", live_speed=0, error=message)

def run_tests():
    try:
        xray = shutil.which("xray.exe") or str(ROOT / "xray.exe")
        curl = shutil.which("curl.exe") or shutil.which("curl")
        links = [x.strip() for x in CONFIGS.read_text(encoding="utf-8-sig").splitlines()
                 if x.strip().lower().startswith("vless://")]
        LOGS.mkdir(exist_ok=True)
        run_id = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with LOCK:
            STATE.update(running=True, rows=[], done=0, total=len(links), started=time.time(), elapsed=0, error="")
        rows = []
        for i, link in enumerate(links, 1):
            name = tester.unquote(tester.urlsplit(link).fragment) or tester.urlsplit(link).hostname
            log = LOGS / tester.safe_filename(i, name)
            try:
                def progress(speed, size, elapsed):
                    with LOCK:
                        STATE.update(current=name, phase="Загрузка", live_speed=round(speed, 2), live_bytes=size)
                with LOCK: STATE.update(current=name, phase="Подключение", live_speed=0, live_bytes=0)
                name, mbps, note = tester.test_one(xray, curl, link, 10, tester.TEST_URLS, log, progress)
            except Exception as e:
                mbps, note = "ERROR", str(e)
            m = re.search(r"ping\s+(\d+)\s*ms", note)
            row = {"index": i, "name": name, "mbps": mbps, "ping": int(m.group(1)) if m else None, "note": note}
            rows.append(row)
            with LOCK:
                STATE.update(rows=list(rows), done=i, elapsed=round(time.time() - STATE["started"], 1), current="", live_speed=0, live_bytes=0)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        exists = HISTORY.exists() and HISTORY.stat().st_size > 0
        with HISTORY.open("a", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["run", "timestamp_utc", "index", "name", "download_mbps", "ping_ms", "note"])
            if not exists: w.writeheader()
            for r in rows: w.writerow({"run": run_id, "timestamp_utc": stamp, "index": r["index"], "name": r["name"], "download_mbps": r["mbps"], "ping_ms": r["ping"] or "", "note": r["note"]})
        with LOCK: STATE["running"] = False; STATE["elapsed"] = round(time.time() - STATE["started"], 1)
    except Exception as e:
        set_error(str(e))

class Handler(BaseHTTPRequestHandler):
    def send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/status":
            with LOCK:
                snapshot = STATE.copy()
                if snapshot["running"]: snapshot["elapsed"] = round(time.time() - snapshot["started"], 1)
                self.send_json(snapshot)
        elif path == "/api/history":
            if HISTORY.exists():
                with HISTORY.open(encoding="utf-8-sig") as f: self.send_json(list(csv.DictReader(f))[-100:])
            else: self.send_json([])
        else:
            data = (ROOT / "dashboard.html").read_bytes(); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/configs":
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8-sig", errors="replace")
            links = [x.strip() for x in body.splitlines() if x.strip().lower().startswith("vless://")]
            CONFIGS.write_text("\n".join(links) + ("\n" if links else ""), encoding="utf-8")
            self.send_json({"ok": True, "count": len(links)}); return
        if path != "/api/start": self.send_json({"error":"not found"}, 404); return
        with LOCK:
            if STATE["running"]: self.send_json({"error":"already running"}, 409); return
        threading.Thread(target=run_tests, daemon=True).start(); self.send_json({"ok": True})
    def log_message(self, *args): pass

if __name__ == "__main__":
    migrate_history()
    server = None
    for port in (8765, 18765, 28765, 0):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler); break
        except PermissionError:
            continue
    if server is None:
        raise SystemExit("Не удалось открыть локальный порт")
    print(f"Dashboard: http://127.0.0.1:{server.server_port}")
    server.serve_forever()
