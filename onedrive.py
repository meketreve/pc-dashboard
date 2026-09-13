"""Status do cliente OneDrive (abraunegg/onedrive) pro painel.

Tudo local: acompanha o journal do servico `onedrive` em tempo real (estado da sync,
uploads/downloads, erros) e le a cota com `onedrive --display-quota` de hora em hora
(o proprio cliente renova o token nessa chamada; este modulo nunca le nem escreve o token).
"""
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

CONFIG = Path.home() / ".config/onedrive/config"
QUOTA_CACHE = Path.home() / ".config/pc-dashboard/onedrive-cota.json"  # sobrevive a reinicios do painel
QUOTA_EVERY = 3600  # --display-quota reescreve o token do cliente: pouca frequencia
LOCAL_EVERY = 600

ACTIVITY = re.compile(
    r"^(?P<verb>Uploading new file|Uploading modified file|Uploading|Downloading file|Deleting item from Microsoft OneDrive|"
    r"Deleting local (?:file|directory)|Creating local directory):\s+(?P<path>.+?)"
    r"(?:\s+\.\.\.\s*(?P<res>done|failed!?))?\s*$")
KIND = {"Uploading": "up", "Downloading": "down", "Deleting": "del", "Creating": "new", "Moving": "move"}


def _sync_dir():
    try:
        for line in CONFIG.read_text().splitlines():
            m = re.match(r'\s*sync_dir\s*=\s*"(.*)"', line)
            if m:
                return os.path.expanduser(m.group(1))
    except OSError:
        pass
    return str(Path.home() / "OneDrive")


class OneDrive:
    def __init__(self):
        self.state = {"installed": False}
        self._syncing_since = None
        self._last_sync = None
        self._last_error = None
        self._offline = False
        self._activity = []
        self._quota = None
        self._local = None
        self._active = None
        if subprocess.run(["which", "onedrive"], capture_output=True).returncode != 0:
            return
        self.state = {"installed": True}
        threading.Thread(target=self._follow, daemon=True).start()
        threading.Thread(target=self._poll, daemon=True).start()

    # --- journal em tempo real ---
    def _follow(self):
        while True:
            try:
                proc = subprocess.Popen(["journalctl", "--user", "-u", "onedrive", "-f", "-n", "400", "-o", "json"],
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                for line in proc.stdout:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    msg = e.get("MESSAGE")
                    if isinstance(msg, str):
                        self._handle(msg.strip(), int(e.get("__REALTIME_TIMESTAMP", 0)) / 1e6)
            except OSError:
                pass
            time.sleep(10)

    def _handle(self, msg, ts):
        if "Starting a sync with Microsoft OneDrive" in msg or "Performing initial synchronisation" in msg:
            self._syncing_since = ts
        elif "Sync with Microsoft OneDrive is complete" in msg:
            self._syncing_since = None
            self._last_sync = ts
        elif msg.startswith("Cannot connect to the Microsoft OneDrive Service") or "service is not reachable" in msg:
            self._offline = True
        elif "connectivity to Microsoft OneDrive service has been restored" in msg or "Successfully reached the Microsoft OneDrive Service" in msg:
            self._offline = False
        if msg.startswith("ERROR") or "ERROR:" in msg:
            self._last_error = {"msg": msg.replace("ERROR:", "").strip()[:160], "t": ts}
        m = ACTIVITY.match(msg)
        if m:
            res = (m.group("res") or "").lower()
            self._activity.insert(0, {
                "kind": KIND.get(m.group("verb").split()[0], "new"),
                "name": os.path.basename(m.group("path").rstrip("/")) or m.group("path"),
                "t": ts,
                "ok": not res.startswith("fail"),
            })
            del self._activity[5:]
        self._publish()

    # --- servico, cota e pasta local ---
    def _poll(self):
        next_local = 0
        try:
            self._quota = json.loads(QUOTA_CACHE.read_text())
            next_quota = self._quota["t"] + QUOTA_EVERY
        except (OSError, ValueError, KeyError, TypeError):
            next_quota = 0
        while True:
            now = time.time()
            self._active = subprocess.run(["systemctl", "--user", "is-active", "onedrive"],
                                          capture_output=True, text=True).stdout.strip()
            if now >= next_local:
                next_local = now + LOCAL_EVERY
                self._local = self._scan(_sync_dir())
            if now >= next_quota and self._active == "active":
                next_quota = now + QUOTA_EVERY
                q = self._read_quota()
                if q:
                    self._quota = q
                    try:
                        QUOTA_CACHE.write_text(json.dumps(q))
                    except OSError:
                        pass
            self._publish()
            time.sleep(10)

    @staticmethod
    def _scan(root):
        files = size = 0
        for dirpath, dirs, names in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for n in names:
                try:
                    size += os.lstat(os.path.join(dirpath, n)).st_size
                    files += 1
                except OSError:
                    pass
        return {"dir": root, "files": files, "size": size}

    @staticmethod
    def _read_quota():
        try:
            out = subprocess.run(["onedrive", "--display-quota"], capture_output=True, text=True, timeout=90).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        vals = dict(re.findall(r"^(Total|Used|Remaining|Deleted):.*\((\d+) bytes\)", out, re.M))
        if "Total" not in vals:
            return None
        return {k.lower(): int(v) for k, v in vals.items()} | {"t": time.time()}

    def _publish(self):
        if self._active not in (None, "active"):
            status = "stopped"
        elif self._offline:
            status = "offline"
        elif self._syncing_since:
            status = "syncing"
        elif self._last_error and (not self._last_sync or self._last_error["t"] > self._last_sync):
            status = "error"
        elif self._last_sync:
            status = "ok"
        else:
            status = "starting"
        self.state = {
            "installed": True,
            "status": status,
            "service": self._active,
            "last_sync": self._last_sync,
            "error": self._last_error,
            "activity": list(self._activity),  # copia: a thread do journal altera a lista
            "quota": self._quota,
            "local": self._local,
        }
