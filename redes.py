"""Contadores das redes sociais pro painel.

Credenciais em ~/.config/pc-dashboard/redes.json (fora do git; relido quando muda).
Historico diario em ~/.config/pc-dashboard/redes-historico.json (pra "+N hoje" e a tendencia de 30 dias).
"""
import datetime
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CONF = Path.home() / ".config/pc-dashboard/redes.json"
HIST = Path.home() / ".config/pc-dashboard/redes-historico.json"
HIST_DAYS = 30
TRACKED = ("followers", "views", "likes")


class ApiError(Exception):
    pass


def _get(url, headers=None, data=None):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        # So a mensagem da API: a URL pode ter a chave e nunca vai pro navegador
        try:
            err = json.loads(e.read() or b"{}")
            msg = err.get("error", {}).get("message") if isinstance(err.get("error"), dict) else err.get("message")
        except ValueError:
            msg = None
        raise ApiError(f"HTTP {e.code}" + (f": {msg}" if msg else "")) from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise ApiError(f"sem conexão ({getattr(e, 'reason', e)})") from None


def _q(**kw):
    return urllib.parse.urlencode({k: v for k, v in kw.items() if v is not None})


class Youtube:
    interval = 600  # cada leitura custa ~5 unidades da cota diaria de 10.000
    API = "https://www.googleapis.com/youtube/v3/"

    def __init__(self, cfg):
        self.cfg = cfg

    def configured(self):
        return bool(self.cfg.get("api_key") and self.cfg.get("handle"))

    def fetch(self):
        key = self.cfg["api_key"]
        d = _get(self.API + "channels?" + _q(part="snippet,statistics,contentDetails", forHandle=self.cfg["handle"], key=key))
        if not d.get("items"):
            raise ApiError(f"canal {self.cfg['handle']} não encontrado")
        ch = d["items"][0]
        st = ch["statistics"]
        # Likes: a API nao da o total do canal, entao soma video por video
        ids, token = [], None
        while len(ids) < 500:
            page = _get(self.API + "playlistItems?" + _q(part="contentDetails", maxResults=50, pageToken=token, key=key,
                                                          playlistId=ch["contentDetails"]["relatedPlaylists"]["uploads"]))
            ids += [it["contentDetails"]["videoId"] for it in page.get("items", [])]
            token = page.get("nextPageToken")
            if not token:
                break
        likes, videos = 0, {}
        for i in range(0, len(ids), 50):
            for v in _get(self.API + "videos?" + _q(part="statistics,snippet", id=",".join(ids[i:i + 50]), key=key)).get("items", []):
                likes += int(v["statistics"].get("likeCount", 0))
                videos[v["id"]] = v
        latest = videos.get(ids[0]) if ids else None
        return {
            "name": ch["snippet"]["title"],
            "handle": self.cfg["handle"],
            "followers": int(st.get("subscriberCount", 0)),
            "views": int(st.get("viewCount", 0)),
            "likes": likes,
            "posts": int(st.get("videoCount", 0)),
            "latest": latest and {
                "title": latest["snippet"]["title"],
                "views": int(latest["statistics"].get("viewCount", 0)),
                "likes": int(latest["statistics"].get("likeCount", 0)),
                "published": latest["snippet"]["publishedAt"],
            },
        }


class Twitch:
    interval = 60  # rapido pra perceber quando a live comeca
    API = "https://api.twitch.tv/helix/"

    def __init__(self, cfg):
        self.cfg = cfg
        self.token = None
        self.user = None

    def configured(self):
        return bool(self.cfg.get("client_id") and self.cfg.get("client_secret") and self.cfg.get("login"))

    def _helix(self, path):
        for _ in range(2):
            if not self.token:
                self.token = _get("https://id.twitch.tv/oauth2/token", data=_q(
                    client_id=self.cfg["client_id"], client_secret=self.cfg["client_secret"],
                    grant_type="client_credentials").encode())["access_token"]
            try:
                return _get(self.API + path, {"Client-Id": self.cfg["client_id"], "Authorization": "Bearer " + self.token})
            except ApiError as e:
                if not str(e).startswith("HTTP 401"):
                    raise
                self.token = None  # token venceu: pede outro e tenta de novo
        raise ApiError("token recusado pela Twitch")

    def fetch(self):
        if not self.user:
            users = self._helix("users?" + _q(login=self.cfg["login"])).get("data") or []
            if not users:
                raise ApiError(f"usuário {self.cfg['login']} não encontrado")
            self.user = users[0]
        uid = self.user["id"]
        followers = self._helix("channels/followers?" + _q(broadcaster_id=uid))["total"]
        live = (self._helix("streams?" + _q(user_id=uid)).get("data") or [None])[0]
        return {
            "name": self.user["display_name"],
            "handle": self.cfg["login"],
            "followers": followers,
            "live": live and {
                "title": live["title"],
                "game": live["game_name"],
                "viewers": live["viewer_count"],
                "started": live["started_at"],
            },
        }


class NaoConfigurado:
    """Instagram e TikTok: ainda sem integracao."""
    interval = 3600

    def __init__(self, cfg):
        self.cfg = cfg

    def configured(self):
        return False


PROVIDERS = {"youtube": Youtube, "twitch": Twitch, "instagram": NaoConfigurado, "tiktok": NaoConfigurado}


class Redes:
    def __init__(self):
        self.data = {}
        self._mtime = None
        self._providers = {}
        self._next = {}
        try:
            self.hist = json.loads(HIST.read_text())
        except (OSError, ValueError):
            self.hist = {}
        threading.Thread(target=self._loop, daemon=True).start()

    def _load_config(self):
        try:
            mtime = CONF.stat().st_mtime
        except OSError:
            mtime = None
        if mtime == self._mtime:
            return
        self._mtime = mtime
        try:
            cfg = json.loads(CONF.read_text()) if mtime else {}
        except (OSError, ValueError):
            cfg = {}
        # Config mudou: recria tudo e busca de novo ja
        self._providers = {name: cls(cfg.get(name, {})) for name, cls in PROVIDERS.items()}
        self._next = {}

    def _loop(self):
        while True:
            self._load_config()
            now = time.time()
            for name, prov in self._providers.items():
                if now < self._next.get(name, 0):
                    continue
                self._next[name] = now + prov.interval
                handle = prov.cfg.get("handle") or prov.cfg.get("login") or prov.cfg.get("usuario")
                if not prov.configured():
                    self.data[name] = {"configured": False, "handle": handle}
                    continue
                try:
                    vals = prov.fetch()
                    self._record(name, vals)
                    self.data[name] = {"configured": True, "ok": True, "updated": now, **vals, **self._trend(name)}
                except Exception as exc:  # erro de rede/API nao pode derrubar a thread
                    prev = self.data.get(name, {})
                    self.data[name] = {**prev, "configured": True, "ok": False, "handle": handle, "error": str(exc)[:200]}
                    self._next[name] = now + min(prov.interval, 120)
            time.sleep(10)

    def _record(self, name, vals):
        day = datetime.date.today().isoformat()
        days = self.hist.setdefault(name, {})
        entry = days.setdefault(day, {})
        for k in TRACKED:
            if isinstance(vals.get(k), int):
                entry.setdefault(k, [vals[k], vals[k]])[1] = vals[k]  # [primeiro, ultimo] do dia
        for old in sorted(days)[:-HIST_DAYS]:
            del days[old]
        try:
            HIST.write_text(json.dumps(self.hist))
        except OSError:
            pass

    def _trend(self, name):
        days = self.hist.get(name, {})
        order = sorted(days)
        if not order:
            return {}
        today, prev = days[order[-1]], (days[order[-2]] if len(order) > 1 else None)
        delta = {}
        for k, (first, last) in today.items():
            base = prev[k][1] if prev and k in prev else first
            delta[k] = last - base
        return {"today": delta, "series": [days[d].get("followers", [None, None])[1] for d in order]}
