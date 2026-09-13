#!/usr/bin/env python3
"""Painel do PC: servidor local (127.0.0.1) com metricas do sistema e atalhos.

Metricas: /api/stats (JSON, atualizado 1x/s por uma thread de coleta).
Atalhos:  POST /api/run/<id>, somente ids definidos em ~/.config/pc-dashboard/atalhos.json.
"""
import json
import os
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psutil

HOST, PORT = "127.0.0.1", 8787
BASE = Path(__file__).resolve().parent
CONFIG = Path.home() / ".config/pc-dashboard/atalhos.json"
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
NET_IFACE = "enp7s0"
DISKS = [("/", "Sistema (NVMe)"), ("/mnt/SSD", "SSD")]

DEFAULT_SHORTCUTS = [
    {"id": "vol_down", "label": "Volume −", "icon": "🔉", "cmd": ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "-5%"]},
    {"id": "vol_up", "label": "Volume +", "icon": "🔊", "cmd": ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "+5%"]},
    {"id": "mute", "label": "Mudo", "icon": "🔇", "cmd": ["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"]},
    {"id": "print", "label": "Print da tela", "icon": "📸", "cmd": ["gnome-screenshot"]},
    {"id": "terminal", "label": "Terminal", "icon": "⌨️", "cmd": ["gnome-terminal"]},
    {"id": "ssd", "label": "Arquivos do SSD", "icon": "📁", "cmd": ["nemo", "/mnt/SSD"]},
    {"id": "steam", "label": "Steam", "icon": "🎮", "cmd": ["steam"]},
    {"id": "monitor", "label": "Monitor do sistema", "icon": "📊", "cmd": ["gnome-system-monitor"]},
    {"id": "sunshine", "label": "Painel Sunshine", "icon": "☀️", "cmd": ["xdg-open", "https://localhost:47990"]},
    {"id": "lock", "label": "Bloquear tela", "icon": "🔒", "cmd": ["cinnamon-screensaver-command", "--lock"]},
]


def load_shortcuts():
    if not CONFIG.exists():
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(json.dumps(DEFAULT_SHORTCUTS, ensure_ascii=False, indent=2) + "\n")
    try:
        items = json.loads(CONFIG.read_text())
        return [s for s in items if isinstance(s, dict) and s.get("id") and isinstance(s.get("cmd"), list)]
    except (OSError, ValueError):
        return DEFAULT_SHORTCUTS


class GpuReader:
    """Mantem um nvidia-smi rodando em loop (-lms) em vez de abrir um processo por segundo."""

    FIELDS = ["util", "enc", "dec", "mem_used", "mem_total", "temp", "power", "power_limit", "fan", "clock"]
    QUERY = ("utilization.gpu,utilization.encoder,utilization.decoder,memory.used,memory.total,"
             "temperature.gpu,power.draw,power.limit,fan.speed,clocks.gr")

    def __init__(self):
        self.latest = None
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                proc = subprocess.Popen(
                    ["nvidia-smi", f"--query-gpu={self.QUERY}", "--format=csv,noheader,nounits", "-lms", "1000"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                for line in proc.stdout:
                    vals = [v.strip() for v in line.split(",")]
                    if len(vals) != len(self.FIELDS):
                        continue
                    self.latest = {k: _num(v) for k, v in zip(self.FIELDS, vals)}
            except OSError:
                pass
            self.latest = None
            time.sleep(5)


class RaplReader:
    """Consumo do pacote da CPU (W) pelo contador de energia RAPL. None se nao tiver permissao."""

    ZONE = Path("/sys/class/powercap/intel-rapl:0")

    def __init__(self):
        self._last = None
        try:
            self._max = int((self.ZONE / "max_energy_range_uj").read_text())
        except (OSError, ValueError):
            self._max = None

    def watts(self, now):
        try:
            e = int((self.ZONE / "energy_uj").read_text())
        except (OSError, ValueError):
            self._last = None
            return None
        prev, self._last = self._last, (e, now)
        if prev is None:
            return None
        de = e - prev[0]
        if de < 0 and self._max:  # contador deu a volta
            de += self._max
        dt = now - prev[1]
        return de / 1e6 / dt if dt > 0 else None


def _num(v):
    try:
        return float(v)
    except ValueError:
        return None


class Collector:
    def __init__(self):
        self.gpu = GpuReader()
        self.rapl = RaplReader()
        self.stats = {}
        self.procs = []
        self.volume = None
        self._proc_cache = {}
        self._last_net = psutil.net_io_counters(pernic=True).get(NET_IFACE)
        self._last_disk = psutil.disk_io_counters()
        self._last_t = time.monotonic()
        psutil.cpu_percent(percpu=True)
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        tick = 0
        while True:
            time.sleep(1)
            tick += 1
            if tick % 2 == 0:
                self._sample_procs()
                self._sample_volume()
            try:
                self.stats = self._sample()
            except Exception as exc:  # nunca derrubar a thread de coleta
                self.stats = {"error": str(exc)}

    def _sample(self):
        now = time.monotonic()
        dt = max(now - self._last_t, 0.001)
        self._last_t = now

        per = psutil.cpu_percent(percpu=True)
        freq = psutil.cpu_freq()
        vm = psutil.virtual_memory()
        sw = psutil.swap_memory()
        temps = psutil.sensors_temperatures()

        net = psutil.net_io_counters(pernic=True).get(NET_IFACE)
        rx = tx = 0.0
        if net and self._last_net:
            rx = (net.bytes_recv - self._last_net.bytes_recv) / dt
            tx = (net.bytes_sent - self._last_net.bytes_sent) / dt
        self._last_net = net

        dio = psutil.disk_io_counters()
        rd = wr = 0.0
        if dio and self._last_disk:
            rd = (dio.read_bytes - self._last_disk.read_bytes) / dt
            wr = (dio.write_bytes - self._last_disk.write_bytes) / dt
        self._last_disk = dio

        disks = []
        for mount, label in DISKS:
            try:
                u = psutil.disk_usage(mount)
                disks.append({"mount": mount, "label": label, "used": u.used, "total": u.total, "pct": u.percent})
            except OSError:
                disks.append({"mount": mount, "label": label, "used": None, "total": None, "pct": None})

        cpu_temp = None
        for t in temps.get("coretemp", []):
            if t.label.startswith("Package"):
                cpu_temp = t.current
        nvme = temps.get("nvme", [])

        return {
            "t": time.time(),
            "uptime": time.time() - psutil.boot_time(),
            "load": os.getloadavg(),
            "cpu": {"pct": sum(per) / len(per), "per": per, "freq": freq.current if freq else None,
                    "temp": cpu_temp, "threads": len(per), "power": self.rapl.watts(now)},
            "mem": {"used": vm.total - vm.available, "total": vm.total, "pct": vm.percent,
                    "swap_used": sw.used, "swap_total": sw.total},
            "gpu": self.gpu.latest,
            "nvme_temp": nvme[0].current if nvme else None,
            "disks": disks,
            "diskio": {"read": rd, "write": wr},
            "net": {"iface": NET_IFACE, "rx": rx, "tx": tx},
            "procs": self.procs,
            "volume": self.volume,
        }

    def _sample_procs(self):
        ncpu = psutil.cpu_count() or 1
        seen = set()
        rows = []
        for p in psutil.process_iter(["name", "memory_info"]):
            seen.add(p.pid)
            cached = self._proc_cache.get(p.pid)
            if cached is None:
                self._proc_cache[p.pid] = p
                p.cpu_percent(None)  # primeira leitura so inicializa
                continue
            try:
                cpu = cached.cpu_percent(None) / ncpu
                mem = p.info["memory_info"].rss if p.info["memory_info"] else 0
                rows.append({"pid": p.pid, "name": p.info["name"] or "?", "cpu": cpu, "mem": mem})
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        for pid in list(self._proc_cache):
            if pid not in seen:
                del self._proc_cache[pid]
        rows.sort(key=lambda r: (r["cpu"], r["mem"]), reverse=True)
        self.procs = rows[:10]

    def _sample_volume(self):
        if not shutil.which("pactl"):
            return
        try:
            vol = subprocess.run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"], capture_output=True, text=True, timeout=2).stdout
            mute = subprocess.run(["pactl", "get-sink-mute", "@DEFAULT_SINK@"], capture_output=True, text=True, timeout=2).stdout
            pct = next((int(tok.rstrip("%")) for tok in vol.split() if tok.endswith("%")), None)
            self.volume = {"pct": pct, "muted": "yes" in mute or "sim" in mute}
        except (OSError, subprocess.SubprocessError, ValueError):
            self.volume = None


COLLECTOR = Collector()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _host_ok(self):
        return self.headers.get("Host", "") in ALLOWED_HOSTS

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, '{"error":"host"}')
        if self.path in ("/", "/index.html"):
            return self._send(200, (BASE / "index.html").read_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/stats":
            return self._send(200, json.dumps(COLLECTOR.stats))
        if self.path == "/api/shortcuts":
            items = [{k: s.get(k) for k in ("id", "label", "icon")} for s in load_shortcuts()]
            return self._send(200, json.dumps(items, ensure_ascii=False))
        self._send(404, '{"error":"not found"}')

    def do_POST(self):
        # Header customizado forca preflight CORS (que nunca aprovamos): outro site no
        # navegador nao consegue disparar atalhos.
        if not self._host_ok() or self.headers.get("X-Dash") != "1":
            return self._send(403, '{"error":"forbidden"}')
        if not self.path.startswith("/api/run/"):
            return self._send(404, '{"error":"not found"}')
        sid = self.path.rsplit("/", 1)[-1]
        item = next((s for s in load_shortcuts() if s["id"] == sid), None)
        if item is None:
            return self._send(404, '{"error":"atalho desconhecido"}')
        try:
            subprocess.Popen(item["cmd"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            return self._send(500, json.dumps({"error": str(exc)}))
        self._send(200, '{"ok":true}')


if __name__ == "__main__":
    load_shortcuts()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
