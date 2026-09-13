#!/usr/bin/env python3
"""Painel do PC: servidor local (127.0.0.1) com metricas do sistema e atalhos.

Metricas: /api/stats (JSON, atualizado 1x/s por uma thread de coleta).
Redes:    /api/redes, contadores do YouTube/Twitch/... (ver redes.py).
Audio:    /api/audio, PCM s16le mono 24 kHz do monitor da saida padrao (o navegador faz a FFT).
Atalhos:  POST /api/run/<id>, somente ids definidos em ~/.config/pc-dashboard/atalhos.json.
"""
import json
import os
import select
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psutil

from redes import Redes

HOST, PORT = "127.0.0.1", 8787
BASE = Path(__file__).resolve().parent
CONFIG = Path.home() / ".config/pc-dashboard/atalhos.json"
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
NET_IFACE = "enp7s0"
DISKS = [("/", "Sistema (NVMe)"), ("/mnt/SSD", "SSD")]
AUDIO_RATE = 24000
MPRIS = "org.mpris.MediaPlayer2"

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


def _busctl(*args):
    out = subprocess.run(["busctl", "--user", "-j", *args], capture_output=True, text=True, timeout=2)
    return json.loads(out.stdout)["data"] if out.returncode == 0 and out.stdout else None


def now_playing():
    """Musica atual pelo MPRIS (Spotify, navegador, VLC...). Prefere quem esta tocando."""
    names = _busctl("call", "org.freedesktop.DBus", "/org/freedesktop/DBus",
                    "org.freedesktop.DBus", "ListNames") or [[]]
    best = None
    for name in (n for n in names[0] if n.startswith(MPRIS + ".")):
        def prop(p):
            return _busctl("get-property", name, "/org/mpris/MediaPlayer2", MPRIS + ".Player", p)
        status = prop("PlaybackStatus")
        if status not in ("Playing", "Paused"):
            continue
        meta = prop("Metadata") or {}
        val = lambda k: (meta.get(k) or {}).get("data")
        artist = val("xesam:artist")
        info = {
            "player": name[len(MPRIS) + 1:].split(".")[0],
            "status": status,
            "title": val("xesam:title") or "",
            "artist": ", ".join(artist) if isinstance(artist, list) else (artist or ""),
            "album": val("xesam:album") or "",
            "art": val("mpris:artUrl") or "",
            "length": (val("mpris:length") or 0) / 1e6,
            "position": (prop("Position") or 0) / 1e6,
        }
        if status == "Playing":
            return info
        best = best or info
    return best


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
        self.media = None
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
                self._sample_media()
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
            "media": self.media,
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

    def _sample_media(self):
        try:
            self.media = now_playing()
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
            self.media = None


COLLECTOR = Collector()
REDES = Redes()


def monitor_source():
    sink = subprocess.run(["pactl", "get-default-sink"], capture_output=True, text=True, timeout=2).stdout.strip()
    return sink + ".monitor" if sink else "@DEFAULT_MONITOR@"


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
        if self.path == "/api/version":  # o painel se recarrega quando o index.html muda
            return self._send(200, json.dumps({"index": (BASE / "index.html").stat().st_mtime}))
        if self.path == "/api/redes":
            return self._send(200, json.dumps(REDES.data, ensure_ascii=False))
        if self.path == "/api/audio":
            return self._stream_audio()
        self._send(404, '{"error":"not found"}')

    def _stream_audio(self):
        # Um parec por cliente, vivo so enquanto o painel estiver lendo.
        proc = subprocess.Popen(
            ["parec", "-d", monitor_source(), "--format=s16le", f"--rate={AUDIO_RATE}", "--channels=1",
             "--latency-msec=20", "--raw"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            fd = proc.stdout.fileno()
            silence = bytes(AUDIO_RATE * 2 // 10)
            while proc.poll() is None:
                # Saida suspensa nao gera amostras: manda silencio pra perceber se o cliente saiu
                if select.select([fd], [], [], 0.25)[0]:
                    data = os.read(fd, 4096)
                    if not data:
                        break
                else:
                    data = silence
                self.wfile.write(data)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            proc.kill()
            proc.wait()

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
