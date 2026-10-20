#!/usr/bin/env python3
"""Sequential VLESS download-speed test through local Xray.

Requires: Python 3.9+, Xray core (xray.exe), curl.exe (included in Windows 10/11).
No config is uploaded; only the selected speed-test URL passes through each tunnel.
"""
import argparse
import csv
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

# Public files. Different VLESS exits may block one provider; script tries each.
TEST_URLS = [
    "https://cachefly.cachefly.net/100mb.test",
    "https://ash-speed.hetzner.com/100MB.bin",
]
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36"


def find_free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def one(query, key, default=""):
    return query.get(key, [default])[0]


def vless_to_xray(link, socks_port):
    """Convert common VLESS URI fields to Xray JSON."""
    u = urlsplit(link.strip())
    if u.scheme.lower() != "vless" or not u.hostname or not u.username or not u.port:
        raise ValueError("not valid vless:// URI")
    q = parse_qs(u.query, keep_blank_values=True)
    network = one(q, "type", "tcp")
    security = one(q, "security", "none")
    stream = {"network": network, "security": security}

    if network == "ws":
        stream["wsSettings"] = {
            "path": one(q, "path", "/"),
            "headers": {"Host": one(q, "host")} if one(q, "host") else {},
        }
    elif network == "grpc":
        stream["grpcSettings"] = {"serviceName": one(q, "serviceName")}
    elif network == "kcp":
        kcp = {k: int(one(q, k)) for k in ("mtu", "tti") if one(q, k)}
        kcp["congestion"] = one(q, "congestion", "false").lower() == "true"
        stream["kcpSettings"] = kcp
    elif network in ("xhttp", "splithttp"):
        settings = {"path": one(q, "path", "/")}
        if one(q, "host"):
            settings["host"] = one(q, "host")
        if one(q, "mode"):
            settings["mode"] = one(q, "mode")
        if one(q, "extra"):
            try:
                settings["extra"] = json.loads(unquote(one(q, "extra")))
            except json.JSONDecodeError:
                pass
        stream["xhttpSettings"] = settings

    server_name = one(q, "sni") or one(q, "host")
    fingerprint = one(q, "fp")
    if security == "reality":
        reality = {"show": False, "serverName": server_name, "publicKey": one(q, "pbk"),
                   "shortId": one(q, "sid"), "spiderX": one(q, "spx", "/")}
        if fingerprint:
            reality["fingerprint"] = fingerprint
        stream["realitySettings"] = reality
    elif security == "tls":
        tls = {"serverName": server_name, "allowInsecure": False}
        if fingerprint:
            tls["fingerprint"] = fingerprint
        if one(q, "alpn"):
            tls["alpn"] = one(q, "alpn").split(",")
        stream["tlsSettings"] = tls

    return {
        "log": {"loglevel": "warning"},
        "inbounds": [{"listen": "127.0.0.1", "port": socks_port, "protocol": "socks",
                      "settings": {"auth": "noauth", "udp": False}}],
        "outbounds": [{"protocol": "vless", "settings": {"vnext": [{
            "address": u.hostname, "port": u.port,
            "users": [{"id": unquote(u.username), "encryption": one(q, "encryption", "none"),
                       "flow": one(q, "flow")}],
        }]}, "streamSettings": stream}],
    }


def wait_for_port(port, proc, seconds=12):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if proc.poll() is not None:
            return False
        with socket.socket() as s:
            s.settimeout(.2)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(.2)
    return False


def safe_filename(index, name):
    bad = '<>:"/\\|?*'
    clean = ''.join('_' if c in bad else c for c in name).strip().rstrip('.')
    return f"{index:02d}_{clean[:80] or 'config'}.log"


def test_one(xray, curl, link, size_mb, test_urls, log_file, progress_callback=None):
    """One bounded download through one Xray SOCKS listener."""
    name = unquote(urlsplit(link).fragment) or urlsplit(link).hostname
    port = find_free_port()
    with tempfile.TemporaryDirectory(prefix="vless-test-") as temp:
        cfg = Path(temp) / "config.json"
        output_file = Path(temp) / "download.bin"
        cfg.write_text(json.dumps(vless_to_xray(link, port), ensure_ascii=False), encoding="utf-8")
        proc = subprocess.Popen([xray, "run", "-c", str(cfg)], stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True)
        try:
            if not wait_for_port(port, proc, seconds=3):
                error = (proc.stderr.read() or "Xray did not start").strip().replace("\n", " ")
                log_file.write_text(f"Name: {name}\nXray did not start: {error}\n", encoding="utf-8")
                return name, "ERROR", "Xray did not start"
            proxy = f"socks5h://127.0.0.1:{port}"
            ping_cmd = [curl, "--silent", "--show-error", "--http1.1", "--connect-timeout", "3",
                        "--max-time", "3", "--proxy", proxy, "--header", "Connection: keep-alive",
                        "--output", "NUL", "--write-out", "%{http_code},%{time_total}",
                        "https://www.google.com/generate_204"]
            ping_run = subprocess.run(ping_cmd, capture_output=True, text=True)
            ping_ms = None
            try:
                ping_status, ping_seconds = ping_run.stdout.strip().rsplit(",", 1)
                if float(ping_seconds) > 0:
                    ping_ms = float(ping_seconds) * 1000
            except (ValueError, AttributeError):
                pass
            test_url = test_urls[0]
            bytes_to_test = size_mb * 1024 * 1024
            streams = 4
            outputs = [Path(temp) / f"download-{i}.bin" for i in range(streams)]
            commands = [[curl, "--silent", "--show-error", "--fail", "--connect-timeout", "8",
                         "--max-time", "10", "--proxy", proxy, "--output", str(out),
                         "--user-agent", USER_AGENT, test_url] for out in outputs]
            started = time.monotonic()
            processes = [subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                         for cmd in commands]
            while any(p.poll() is None for p in processes):
                elapsed = max(time.monotonic() - started, 0.001)
                size = sum(out.stat().st_size for out in outputs if out.exists())
                if progress_callback:
                    progress_callback(size * 8 / elapsed / 1_000_000, size, elapsed)
                time.sleep(.2)
            results = [p.communicate() for p in processes]
            received = sum(out.stat().st_size for out in outputs if out.exists())
            elapsed = max(time.monotonic() - started, 0.001)
            ping_note = f"ping {ping_ms:.0f} ms; " if ping_ms is not None and ping_ms > 0 else "ping n/a; "
            log_file.write_text(f"Name: {name}\nPing GET: {ping_ms or 'n/a'} ms\n"
                                f"URL: {test_url}\nStreams: {streams}\nReceived: {received}\n"
                                f"Curl errors:\n" + "\n".join(err for _, err in results), encoding="utf-8")
            if received >= 256 * 1024:
                return name, f"{received * 8 / elapsed / 1_000_000:.2f}", ping_note + f"{streams} streams"
            return name, "ERROR", "download failed (all streams)"
        except Exception as e:
            return name, "ERROR", str(e)[:200]
        finally:
            proc.terminate()
            try: proc.wait(timeout=2)
            except subprocess.TimeoutExpired: proc.kill()


def main():
    ap = argparse.ArgumentParser(description="VLESS download speed test through Xray")
    ap.add_argument("configs", type=Path, help="text file: one vless:// URI per line")
    ap.add_argument("--xray", default="xray.exe", help="path to xray.exe")
    ap.add_argument("--size-mb", type=int, default=100, help="download size per config (default 100)")
    ap.add_argument("--csv", type=Path, default=Path("vless_results.csv"))
    ap.add_argument("--logs-dir", type=Path, default=Path("vless_logs"))
    ap.add_argument("--url", action="append", default=[],
                    help="custom test URL; repeat for fallbacks; may include {bytes}")
    args = ap.parse_args()
    bundled_xray = Path(__file__).resolve().parent / args.xray
    xray = (shutil.which(args.xray) or (str(Path(args.xray)) if Path(args.xray).is_file() else None)
            or (str(bundled_xray) if bundled_xray.is_file() else None))
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not xray or not curl:
        sys.exit("Need xray.exe and curl.exe. Put xray.exe beside script or add it to PATH.")
    links = [x.strip() for x in args.configs.read_text(encoding="utf-8-sig").splitlines()
             if x.strip().lower().startswith("vless://")]
    if not links:
        sys.exit("No vless:// links found.")
    args.logs_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for index, link in enumerate(links, 1):
        print(f"[{index}/{len(links)}] testing...", end=" ", flush=True)
        log_file = args.logs_dir / safe_filename(index, unquote(urlsplit(link).fragment))
        urls = args.url or TEST_URLS
        name, mbps, note = test_one(xray, curl, link, args.size_mb, urls, log_file)
        print(f"{name}: {mbps} Mbps ({note})")
        rows.append({"name": name, "download_mbps": mbps, "note": note})
    with args.csv.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["name", "download_mbps", "note"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nSaved: {args.csv.resolve()}")


if __name__ == "__main__":
    main()
