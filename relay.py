#!/usr/bin/env python3
"""
Signal Media Relay
==================

Empfaengt Webhooks von Sonarr, Radarr und/oder Jellyfin und kuendigt neue
Filme/Folgen in einer Signal-Gruppe (oder an eine Nummer) an. Der Versand
laeuft ueber die signal-cli-rest-api (bbernhard/signal-cli-rest-api).

Endpunkte (POST, JSON):
    /sonarr    Sonarr  -> Settings > Connect > Webhook
    /radarr    Radarr  -> Settings > Connect > Webhook
    /jellyfin  Jellyfin Webhook-Plugin (Generic Destination, Template siehe README)
    /health    GET, Statuspruefung

Auth (eine der Varianten):
    - Basic Auth, Passwort = RELAY_TOKEN (Sonarr/Radarr: Felder Username/Password)
    - Header "X-Relay-Token: <RELAY_TOKEN>" (Jellyfin: Header im Webhook-Plugin)
    - ?token=<RELAY_TOKEN> in der URL (Fallback, landet ggf. in fremden Logs)

Optional (REQUESTS_ENABLED=true): liest ueber den WebSocket der signal-cli-rest-api
mit und verarbeitet "!request <Titel>" aus der Gruppe (oder per Direktnachricht von
Gruppenmitgliedern) – sucht in Radarr/Sonarr, fuegt den Titel hinzu und antwortet.

Nur Python-Standardbibliothek, keine Abhaengigkeiten.
"""

from __future__ import annotations

__version__ = "0.8.0"

import base64
import binascii
import difflib
import hashlib
import hmac
import html
import json
import logging
import os
import queue
import re
import signal
import socket
import ssl
import struct
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------- #
# Konfiguration
# --------------------------------------------------------------------------- #


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, "").strip()
    return value if value else default


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes", "ja", "on")


def _env_int(name: str, default: int) -> int:
    value = _env(name)
    try:
        return int(value) if value is not None else default
    except ValueError:
        sys.exit(f"Konfigurationsfehler: {name} muss eine Zahl sein (ist: {value!r})")


SIGNAL_API_URL = (_env("SIGNAL_API_URL", "http://signal-api:8080") or "").rstrip("/")
SIGNAL_NUMBER = _env("SIGNAL_NUMBER")
SIGNAL_RECIPIENT = _env("SIGNAL_RECIPIENT")
RELAY_TOKEN = _env("RELAY_TOKEN", "")
LISTEN_HOST = _env("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = _env_int("LISTEN_PORT", 8090)
AGGREGATE_SECONDS = _env_int("AGGREGATE_SECONDS", 60)
DEDUPE_HOURS = _env_int("DEDUPE_HOURS", 24)
MAX_EPISODES_LISTED = _env_int("MAX_EPISODES_LISTED", 10)
OVERVIEW_MAX_CHARS = _env_int("OVERVIEW_MAX_CHARS", 350)
ANNOUNCE_UPGRADES = _env_bool("ANNOUNCE_UPGRADES", False)
ANNOUNCE_TESTS = _env_bool("ANNOUNCE_TESTS", False)
SEND_POSTERS = _env_bool("SEND_POSTERS", True)
JELLYFIN_URL = (_env("JELLYFIN_URL", "") or "").rstrip("/")
JELLYFIN_API_KEY = _env("JELLYFIN_API_KEY", "")
LOG_LEVEL = (_env("LOG_LEVEL", "INFO") or "INFO").upper()
ALLOW_INSECURE_NO_TOKEN = _env_bool("ALLOW_INSECURE_NO_TOKEN", False)
# Nur von diesen Hosts (inkl. Subdomains) werden Poster geladen; JELLYFIN_URL wird automatisch ergaenzt.
POSTER_ALLOWED_HOSTS = {
    h.strip().lower().lstrip(".")
    for h in (_env("POSTER_ALLOWED_HOSTS", "artworks.thetvdb.com,image.tmdb.org,assets.fanart.tv") or "").split(",")
    if h.strip()
}
if JELLYFIN_URL:
    _jf_host = urllib.parse.urlsplit(JELLYFIN_URL).hostname
    if _jf_host:
        POSTER_ALLOWED_HOSTS.add(_jf_host.lower())

MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_POSTER_BYTES = 8 * 1024 * 1024
MAX_FIELD_CHARS = 300
MAX_OUTBOX = 500
REQUEST_TIMEOUT_SECONDS = 15

# --- !request-Feature (Anfragen aus der Signal-Gruppe an Radarr/Sonarr) ----
REQUESTS_ENABLED = _env_bool("REQUESTS_ENABLED", False)
RADARR_URL = (_env("RADARR_URL", "") or "").rstrip("/")
RADARR_API_KEY = _env("RADARR_API_KEY", "")
RADARR_QUALITY_PROFILE = _env("RADARR_QUALITY_PROFILE", "")      # Name oder ID, leer = erstes Profil
RADARR_ROOT_FOLDER = _env("RADARR_ROOT_FOLDER", "")              # Pfad, leer = erster Root-Folder
RADARR_MINIMUM_AVAILABILITY = _env("RADARR_MINIMUM_AVAILABILITY", "released")
SONARR_URL = (_env("SONARR_URL", "") or "").rstrip("/")
SONARR_API_KEY = _env("SONARR_API_KEY", "")
SONARR_QUALITY_PROFILE = _env("SONARR_QUALITY_PROFILE", "")
SONARR_ROOT_FOLDER = _env("SONARR_ROOT_FOLDER", "")
SONARR_MONITOR = _env("SONARR_MONITOR", "all")                   # all, future, missing, firstSeason, ...
REQUEST_LIMIT_PER_DAY = _env_int("REQUEST_LIMIT_PER_DAY", 5)     # erfolgreiche Anfragen pro Person / 24 h, 0 = unbegrenzt
REQUEST_VIA_DM = _env_bool("REQUEST_VIA_DM", True)               # Direktnachrichten von Gruppenmitgliedern annehmen
REQUEST_ALLOWED_SENDERS = {                                       # leer = alle Gruppenmitglieder
    s.strip() for s in (_env("REQUEST_ALLOWED_SENDERS", "") or "").split(",") if s.strip()
}
REQUEST_ADMINS = {                                                # duerfen auch vom Profil abgelehnte Releases holen, kein Limit
    s.strip() for s in (_env("REQUEST_ADMINS", "") or "").split(",") if s.strip()
}
RELEASES_SHOWN = _env_int("RELEASES_SHOWN", 8)                    # so viele Releases zeigt !releases
RELEASE_SEARCH_TIMEOUT = _env_int("RELEASE_SEARCH_TIMEOUT", 180)  # Sekunden fuer die Indexer-Suche
REQUEST_MAX_AGE_SECONDS = 3600      # aeltere (z. B. nachgelieferte) Befehle ignorieren
SELECTION_TTL_SECONDS = 600         # so lange gilt eine Trefferliste fuer "!request <Nr>"
COMMANDS_PER_MINUTE = _env_int("REQUEST_COMMANDS_PER_MINUTE", 6)   # Spam-Bremse pro Person
MAX_WS_MESSAGE_BYTES = 4 * 1024 * 1024
MAX_ARR_RESPONSE_BYTES = 16 * 1024 * 1024

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)-7s %(message)s",
)
log = logging.getLogger("relay")


# --------------------------------------------------------------------------- #
# Datenmodelle
# --------------------------------------------------------------------------- #


@dataclass
class Poster:
    url: str
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class OutgoingMessage:
    text: str
    poster: Poster | None = None
    recipient: str | None = None     # None = SIGNAL_RECIPIENT
    quote: dict | None = None        # quote_timestamp / quote_author / quote_message


@dataclass
class EpisodeGroup:
    series: str
    year: str
    season: int
    source: str
    episodes: dict[int, str] = field(default_factory=dict)
    qualities: set[str] = field(default_factory=set)
    poster: Poster | None = None
    requester: str = ""
    due: float = 0.0                 # Zeitpunkt, ab dem die gesammelten Folgen gepostet werden


USER_AGENT = f"signal-media-relay/{__version__}"


def _norm(text: str) -> str:
    """Vergleichsform: Akzente weg, nur Buchstaben/Ziffern – auch fuer nicht-lateinische Titel
    (vorher wurden z. B. japanische Titel zu "" und kollidierten in der Duplikaterkennung)."""
    text = unicodedata.normalize("NFKD", (text or "").casefold())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[\W_]+", "", text)


# Signal "styled" Text: *kursiv*, **fett**, ~durchgestrichen~, `mono`, ||Spoiler||.
# Fremde Texte (Titel, Namen, Release-Namen) duerfen das Layout nicht kaputtmachen.
_STYLE_MARKERS = str.maketrans({"*": "∗", "~": "∼", "`": "ˋ", "|": "∣"})


def _plain(text: str) -> str:
    return (text or "").translate(_STYLE_MARKERS)


def _truncate(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0] + "…"


def _to_int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩]")


def _clean(value, limit: int = MAX_FIELD_CHARS) -> str:
    """Fremde Strings entschaerfen: Steuerzeichen/Zeilenumbrueche/Bidi raus, Laenge begrenzen."""
    if value is None:
        return ""
    text = _CONTROL_CHARS.sub(" ", str(value))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _redact(text: str) -> str:
    return re.sub(r"(token=)[^&\s]+", r"\1***", text)


# --------------------------------------------------------------------------- #
# Ausgehende HTTP-Requests (abgesichert gegen SSRF)
# --------------------------------------------------------------------------- #


def _url_allowed(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    host = parts.hostname.lower()
    return any(host == h or host.endswith("." + h) for h in POSTER_ALLOWED_HOSTS)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirects nur auf erlaubte Hosts; Auth-Header nie an fremde Hosts weiterreichen."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _url_allowed(newurl):
            raise urllib.error.URLError(f"Redirect auf nicht erlaubte URL blockiert: {newurl}")
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            for name in list(new.headers):
                if name.lower() in ("authorization", "x-emby-token"):
                    del new.headers[name]
        return new


def _build_opener(redirects: bool) -> urllib.request.OpenerDirector:
    # Bewusst kein build_opener(): der wuerde auch file://, ftp:// und data: unterstuetzen.
    opener = urllib.request.OpenerDirector()
    handlers = [urllib.request.UnknownHandler(), urllib.request.HTTPHandler(),
                urllib.request.HTTPSHandler(), urllib.request.HTTPDefaultErrorHandler(),
                urllib.request.HTTPErrorProcessor()]
    if redirects:
        handlers.append(_SafeRedirectHandler())
    for handler in handlers:
        opener.add_handler(handler)
    return opener


_FETCH_OPENER = _build_opener(redirects=True)    # Poster / Jellyfin
_SIGNAL_OPENER = _build_opener(redirects=False)  # signal-cli-rest-api


def _http_get(url: str, headers: dict[str, str] | None = None, timeout: float = 10.0,
              max_bytes: int = MAX_POSTER_BYTES) -> tuple[bytes, str]:
    if not _url_allowed(url):
        raise ValueError("URL nicht erlaubt (Schema/Host nicht in POSTER_ALLOWED_HOSTS)")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with _FETCH_OPENER.open(req, timeout=timeout) as resp:
        content_type = resp.headers.get("Content-Type", "")
        data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("Antwort zu gross")
    return data, content_type


def _fetch_poster_b64(poster: Poster) -> str | None:
    try:
        # Grosszuegiger Timeout: Jellyfin skaliert Bilder neuer Titel beim ersten Abruf erst (kann >10 s dauern)
        data, content_type = _http_get(poster.url, poster.headers, timeout=30)
        if not content_type.lower().startswith("image/"):
            raise ValueError(f"kein Bild (Content-Type {content_type!r})")
        return base64.b64encode(data).decode("ascii")
    except Exception as exc:  # noqa: BLE001 - Poster ist optional
        log.warning("Poster konnte nicht geladen werden (%s): %s", poster.url, exc)
        return None


# --------------------------------------------------------------------------- #
# Signal-Versand
# --------------------------------------------------------------------------- #


def send_signal(msg: OutgoingMessage) -> None:
    payload: dict = {
        "message": msg.text,
        "number": SIGNAL_NUMBER,
        "recipients": [msg.recipient or SIGNAL_RECIPIENT],
        "text_mode": "styled",
    }
    if msg.quote:
        payload.update(msg.quote)
    if SEND_POSTERS and msg.poster:
        b64 = _fetch_poster_b64(msg.poster)
        if b64:
            payload["base64_attachments"] = [b64]

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{SIGNAL_API_URL}/v2/send",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with _SIGNAL_OPENER.open(req, timeout=60) as resp:
        resp.read()


# --------------------------------------------------------------------------- #
# Relay-Kern: Deduplizierung, Buendelung, Versand-Queue
# --------------------------------------------------------------------------- #


class Relay:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, EpisodeGroup] = {}
        self._seen: dict[str, float] = {}
        self._requested: dict[str, tuple[str, float]] = {}   # "movie:<tmdbId>"/"series:<tvdbId>" -> (Name, Zeit)
        self._outbox: "queue.Queue[OutgoingMessage]" = queue.Queue(maxsize=MAX_OUTBOX)
        threading.Thread(target=self._sender_loop, name="signal-sender", daemon=True).start()
        # Ein einziger Thread verschickt faellige Folgen-Buendel. Frueher: ein Timer-Thread pro Serie –
        # bei einem grossen Import (viele Serien gleichzeitig) sprengte das pids_limit des Containers.
        threading.Thread(target=self._flusher_loop, name="episode-flusher", daemon=True).start()

    def _flusher_loop(self) -> None:
        while True:
            time.sleep(1)
            now = time.time()
            with self._lock:
                due = [k for k, g in self._pending.items() if g.due <= now]
            for key in due:
                self._flush(key)

    def _enqueue(self, msg: OutgoingMessage) -> None:
        try:
            self._outbox.put_nowait(msg)
        except queue.Full:
            log.error("Versand-Queue voll (%d) – Nachricht verworfen: %s", MAX_OUTBOX, msg.text.splitlines()[0])

    def send(self, text: str, *, recipient: str | None = None, quote: dict | None = None,
             poster: Poster | None = None) -> None:
        self._enqueue(OutgoingMessage(text, poster, recipient, quote))

    # -- Wer hat was angefragt? (fuer "Angefragt von ..." in der Ankuendigung) --
    def note_requester(self, key: str, name: str) -> None:
        now = time.time()
        with self._lock:
            for k in [k for k, (_, t) in self._requested.items() if now - t > 30 * 86400]:
                del self._requested[k]
            self._requested.setdefault(key, (name, now))   # die erste anfragende Person bleibt genannt

    def pop_requester(self, key: str) -> str:
        if not key:
            return ""
        with self._lock:
            entry = self._requested.pop(key, None)
        return entry[0] if entry else ""

    def wait_until_sent(self, timeout: float) -> None:
        deadline = time.time() + timeout
        while self._outbox.unfinished_tasks and time.time() < deadline:
            time.sleep(0.2)

    # -- Deduplizierung ---------------------------------------------------- #
    def _is_dupe(self, key: str) -> bool:
        """True, wenn key innerhalb von DEDUPE_HOURS schon angekuendigt wurde."""
        if DEDUPE_HOURS <= 0:
            return False
        now = time.time()
        ttl = DEDUPE_HOURS * 3600
        with self._lock:
            for k in [k for k, t in self._seen.items() if now - t > ttl]:
                del self._seen[k]
            if key in self._seen:
                return True
            self._seen[key] = now
            return False

    # -- Filme -------------------------------------------------------------- #
    def add_movie(self, *, title: str, year: str, overview: str, quality: str,
                  poster: Poster | None, source: str, requester_key: str = "") -> None:
        if self._is_dupe(f"movie|{_norm(title)}|{year}"):
            log.info("Film bereits angekuendigt, uebersprungen: %s (%s)", title, year)
            return
        requester = self.pop_requester(requester_key)
        lines = [f"🎬 **Neuer Film:** {_plain(title)}" + (f" ({_plain(year)})" if year else "")]
        if quality:
            lines.append(f"Qualität: {_plain(quality)}")
        if requester:
            lines.append(f"🙋 Angefragt von {_plain(requester)}")
        if overview:
            lines.append("")
            lines.append(f"*{_plain(_truncate(overview, OVERVIEW_MAX_CHARS))}*")
        log.info("[%s] Film: %s (%s)", source, title, year)
        self._enqueue(OutgoingMessage("\n".join(lines), poster))

    # -- Episoden (gebuendelt pro Serie + Staffel) -------------------------- #
    def add_episodes(self, *, series: str, year: str, episodes: list[tuple[int, int, str]],
                     quality: str, poster: Poster | None, source: str, requester_key: str = "") -> None:
        """episodes: Liste aus (staffel, folge, titel)."""
        requester = self.pop_requester(requester_key)
        for season, number, title in episodes:
            if self._is_dupe(f"ep|{_norm(series)}|{season}|{number}"):
                log.info("Folge bereits angekuendigt, uebersprungen: %s S%02dE%02d", series, season, number)
                continue
            gkey = f"{_norm(series)}|{season}"
            with self._lock:
                group = self._pending.get(gkey)
                if group is None:
                    group = EpisodeGroup(series=series, year=year, season=season, source=source,
                                         poster=poster, due=time.time() + max(AGGREGATE_SECONDS, 0))
                    self._pending[gkey] = group
                group.episodes[number] = title or group.episodes.get(number, "")
                if quality:
                    group.qualities.add(quality)
                if group.poster is None and poster is not None:
                    group.poster = poster
                if requester and not group.requester:
                    group.requester = requester
            log.info("[%s] Folge vorgemerkt: %s S%02dE%02d", source, series, season, number)

    def _flush(self, gkey: str) -> None:
        with self._lock:
            group = self._pending.pop(gkey, None)
        if not group or not group.episodes:
            return
        self._enqueue(OutgoingMessage(self._format_group(group), group.poster))

    @staticmethod
    def _format_group(g: EpisodeGroup) -> str:
        count = len(g.episodes)
        label = "Neue Folge" if count == 1 else f"{count} neue Folgen"
        season_txt = "Specials" if g.season == 0 else f"Staffel {g.season}"
        lines = [f"📺 **{label}:** {_plain(g.series)} – {season_txt}"]
        numbers = sorted(g.episodes)
        for num in numbers[:MAX_EPISODES_LISTED]:
            title = g.episodes[num]
            lines.append(f"S{g.season:02d}E{num:02d}" + (f" – {_plain(title)}" if title else ""))
        if count > MAX_EPISODES_LISTED:
            lines.append(f"… und {count - MAX_EPISODES_LISTED} weitere")
        if g.qualities:
            lines.append(f"Qualität: {_plain(', '.join(sorted(g.qualities)))}")
        if g.requester:
            lines.append(f"🙋 Angefragt von {_plain(g.requester)}")
        return "\n".join(lines)

    # -- Sonstiges ----------------------------------------------------------- #
    def add_text(self, text: str) -> None:
        self._enqueue(OutgoingMessage(text))

    def flush_all(self) -> None:
        with self._lock:
            keys = list(self._pending)
        for key in keys:
            self._flush(key)

    def _sender_loop(self) -> None:
        while True:
            msg = self._outbox.get()
            try:
                self._deliver(msg)
            finally:
                self._outbox.task_done()

    @staticmethod
    def _deliver(msg: OutgoingMessage) -> None:
        for attempt in range(1, 5):
            try:
                send_signal(msg)
                log.info("Signal-Nachricht gesendet: %s", msg.text.splitlines()[0])
                return
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:500]
                log.error("Signal-API Fehler %s (Versuch %d/4): %s", exc.code, attempt, detail)
                if 400 <= exc.code < 500 and exc.code != 429:
                    if msg.quote:   # z. B. Zitat-Autor unbekannt -> ohne Zitat nochmal
                        log.warning("Senden mit Zitat abgelehnt – neuer Versuch ohne Zitat")
                        msg.quote = None
                        continue
                    log.error("Dauerhafter Fehler (Konfiguration? SIGNAL_NUMBER/SIGNAL_RECIPIENT pruefen) "
                              "– Nachricht verworfen: %s", msg.text.splitlines()[0])
                    return
            except Exception as exc:  # noqa: BLE001
                log.error("Signal-Versand fehlgeschlagen (Versuch %d/4): %s", attempt, exc)
            if attempt < 4:
                time.sleep(2 ** attempt)
        log.error("Nachricht endgueltig verworfen: %s", msg.text.splitlines()[0])


RELAY: Relay | None = None


# --------------------------------------------------------------------------- #
# Payload-Parser
# --------------------------------------------------------------------------- #


def _arr_poster(images) -> Poster | None:
    if not isinstance(images, list):
        return None
    for img in images:
        if not isinstance(img, dict):
            continue
        url = img.get("remoteUrl")
        if str(img.get("coverType") or "").lower() == "poster" and isinstance(url, str):
            if _url_allowed(url):
                return Poster(url)
            log.warning("Poster-URL nicht erlaubt, ignoriert: %s", _clean(url, 200))
    return None


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value) -> list:
    return value if isinstance(value, list) else []


def handle_test(source: str) -> str:
    log.info("Test-Event von %s empfangen", source)
    if ANNOUNCE_TESTS:
        RELAY.add_text(f"✅ Verbindungstest von {source} erfolgreich.")
    return "test ok"


def handle_sonarr(data: dict) -> str:
    event = data.get("eventType", "")
    if event == "Test":
        return handle_test("Sonarr")
    if event != "Download":
        return f"ignoriert: {event}"
    if data.get("isUpgrade") and not ANNOUNCE_UPGRADES:
        return "ignoriert: Upgrade"

    series = _dict(data.get("series"))
    title = _clean(series.get("title")) or "Unbekannte Serie"
    episodes = []
    for ep in _list(data.get("episodes")):
        ep = _dict(ep)
        season, number = _to_int(ep.get("seasonNumber")), _to_int(ep.get("episodeNumber"))
        if season is None or number is None or not (0 <= season < 10000 and 0 <= number < 100000):
            continue
        episodes.append((season, number, _clean(ep.get("title"))))
    if not episodes:
        return "ignoriert: keine Episoden"

    quality = _clean(_dict(data.get("episodeFile")).get("quality"), 60)
    tvdb_id = _to_int(series.get("tvdbId"))
    RELAY.add_episodes(series=title, year=_clean(series.get("year"), 4), episodes=episodes,
                       quality=quality, poster=_arr_poster(series.get("images")), source="Sonarr",
                       requester_key=f"series:{tvdb_id}" if tvdb_id else "")
    return "ok"


def handle_radarr(data: dict) -> str:
    event = data.get("eventType", "")
    if event == "Test":
        return handle_test("Radarr")
    if event != "Download":
        return f"ignoriert: {event}"
    if data.get("isUpgrade") and not ANNOUNCE_UPGRADES:
        return "ignoriert: Upgrade"

    movie = _dict(data.get("movie"))
    tmdb_id = _to_int(movie.get("tmdbId"))
    RELAY.add_movie(
        requester_key=f"movie:{tmdb_id}" if tmdb_id else "",
        title=_clean(movie.get("title")) or "Unbekannter Film",
        year=_clean(movie.get("year"), 4),
        overview=_clean(movie.get("overview"), 2000),
        quality=_clean(_dict(data.get("movieFile")).get("quality"), 60),
        poster=_arr_poster(movie.get("images")),
        source="Radarr",
    )
    return "ok"


def _jellyfin_headers() -> dict[str, str]:
    return {
        "Authorization": f'MediaBrowser Token="{JELLYFIN_API_KEY}"',
        "X-Emby-Token": JELLYFIN_API_KEY,
    }


_JELLYFIN_ID = re.compile(r"[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _jellyfin_id(value) -> str:
    """Nur echte Jellyfin-IDs (GUID) zulassen – verhindert Pfadmanipulation mit dem API-Key."""
    value = str(value or "").strip()
    return value if _JELLYFIN_ID.fullmatch(value) else ""


def _jf_text(value, limit: int = MAX_FIELD_CHARS) -> str:
    # Das Webhook-Plugin (Handlebars) HTML-escaped Werte: "Katz &amp; Maus" -> "Katz & Maus"
    return _clean(html.unescape(str(value or "")), limit)


def _jellyfin_poster(item_id: str) -> Poster | None:
    if not (JELLYFIN_URL and item_id):
        return None
    return Poster(
        f"{JELLYFIN_URL}/Items/{item_id}/Images/Primary?maxHeight=900&quality=85",
        _jellyfin_headers() if JELLYFIN_API_KEY else {},
    )


def _jellyfin_overview(item_id: str) -> str:
    """Overview per API nachladen (im Webhook-Template ist es wegen JSON-Escaping heikel)."""
    if not (JELLYFIN_URL and JELLYFIN_API_KEY and item_id):
        return ""
    try:
        url = f"{JELLYFIN_URL}/Items?ids={item_id}&fields=Overview"
        raw, _ = _http_get(url, _jellyfin_headers(), timeout=5, max_bytes=MAX_BODY_BYTES)
        items = _list(_dict(json.loads(raw)).get("Items"))
        return _clean(_dict(items[0]).get("Overview"), 2000) if items else ""
    except Exception as exc:  # noqa: BLE001
        log.warning("Jellyfin-Overview nicht abrufbar: %s", exc)
        return ""


def handle_jellyfin(data: dict) -> str:
    ntype = _clean(data.get("NotificationType"), 50)
    if ntype == "Test" or data.get("Event") == "Test":
        return handle_test("Jellyfin")
    if ntype and ntype != "ItemAdded":
        return f"ignoriert: {ntype}"

    item_type = _clean(data.get("ItemType"), 50)
    if item_type == "Movie":
        item_id = _jellyfin_id(data.get("ItemId"))
        RELAY.add_movie(
            title=_jf_text(data.get("Name")) or "Unbekannter Film",
            year=_jf_text(data.get("Year"), 4),
            overview=_jf_text(data.get("Overview"), 2000) or _jellyfin_overview(item_id),
            quality="",
            poster=_jellyfin_poster(item_id),
            source="Jellyfin",
        )
        return "ok"

    if item_type == "Episode":
        season = _to_int(data.get("SeasonNumber"))
        number = _to_int(data.get("EpisodeNumber"))
        if season is None or number is None or not (0 <= season < 10000 and 0 <= number < 100000):
            return "ignoriert: Staffel/Folge fehlt"
        RELAY.add_episodes(
            series=_jf_text(data.get("SeriesName")) or "Unbekannte Serie",
            year=_jf_text(data.get("Year"), 4),
            episodes=[(season, number, _jf_text(data.get("Name")))],
            quality="",
            poster=_jellyfin_poster(_jellyfin_id(data.get("SeriesId")) or _jellyfin_id(data.get("ItemId"))),
            source="Jellyfin",
        )
        return "ok"

    return f"ignoriert: ItemType {item_type or '?'}"


ROUTES = {"/sonarr": handle_sonarr, "/radarr": handle_radarr, "/jellyfin": handle_jellyfin}


# --------------------------------------------------------------------------- #
# !request: Anfragen aus der Signal-Gruppe an Radarr/Sonarr
# --------------------------------------------------------------------------- #


def _mask_number(value: str | None) -> str:
    """Telefonnummern/UUIDs nur gekuerzt loggen."""
    value = value or ""
    return f"{value[:3]}…{value[-2:]}" if len(value) > 6 else "…"


def _internal_group_id(recipient: str) -> str:
    """'group.<base64>' (REST-API-ID) -> interne Signal-Gruppen-ID, wie sie in eingehenden Nachrichten steht."""
    if not (recipient or "").startswith("group."):
        return ""
    try:
        return base64.b64decode(recipient[6:], validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return ""


class WebSocketClient:
    """Minimaler WebSocket-Client (RFC 6455) – reicht fuer /v1/receive der signal-cli-rest-api."""

    _GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, url: str, timeout: float = 30.0) -> None:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("ws", "wss") or not parts.hostname:
            raise ValueError(f"ungueltige WebSocket-URL: {url}")
        secure = parts.scheme == "wss"
        port = parts.port or (443 if secure else 80)
        sock = socket.create_connection((parts.hostname, port), timeout=timeout)
        if secure:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parts.hostname)
        self.sock = sock
        self._buf = b""
        self._in_frame = False

        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        host = parts.hostname if parts.port is None else f"{parts.hostname}:{parts.port}"
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                      f"User-Agent: {USER_AGENT}\r\n\r\n").encode())
        while b"\r\n\r\n" not in self._buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("Handshake abgebrochen")
            self._buf += chunk
            if len(self._buf) > 65536:
                raise ConnectionError("Handshake-Antwort zu gross")
        head, self._buf = self._buf.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        status = lines[0].split()
        if len(status) < 2 or status[1] != "101":
            raise ConnectionError(f"WebSocket abgelehnt: {lines[0]}")
        headers = {k.strip().lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:])}
        expected = base64.b64encode(hashlib.sha1((key + self._GUID).encode()).digest()).decode()
        if headers.get("sec-websocket-accept") != expected:
            raise ConnectionError("WebSocket-Handshake ungueltig")

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self._buf)))
            if not chunk:
                raise ConnectionError("Verbindung geschlossen")
            self._buf += chunk
        data, self._buf = self._buf[:n], self._buf[n:]
        return data

    def _read_frame(self) -> tuple[bool, int, bytes]:
        b1, b2 = self._read_exact(2)
        self._in_frame = True
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        if length > MAX_WS_MESSAGE_BYTES:
            raise ConnectionError("WebSocket-Frame zu gross")
        mask = self._read_exact(4) if b2 & 0x80 else b""
        payload = self._read_exact(length) if length else b""
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._in_frame = False
        return bool(b1 & 0x80), b1 & 0x0F, payload

    def _send(self, opcode: int, payload: bytes = b"") -> None:
        n = len(payload)
        header = bytes([0x80 | opcode])
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", n)
        mask = os.urandom(4)   # Client-Frames muessen maskiert sein
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def recv(self) -> str | None:
        """Naechste Text-Nachricht; None wenn der Server die Verbindung schliesst."""
        fragments: list[bytes] = []
        unanswered_pings = 0
        while True:
            try:
                fin, opcode, payload = self._read_frame()
            except socket.timeout:
                if self._in_frame or self._buf or fragments:
                    raise ConnectionError("Timeout mitten in einer Nachricht")
                if unanswered_pings >= 2:
                    raise ConnectionError("keine Antwort auf Ping")
                self._send(0x9, b"keepalive")
                unanswered_pings += 1
                continue
            unanswered_pings = 0
            if opcode == 0x8:                       # close
                try:
                    self._send(0x8, payload[:2])
                except OSError:
                    pass
                return None
            if opcode == 0x9:                       # ping -> pong
                self._send(0xA, payload)
                continue
            if opcode == 0xA:                       # pong
                continue
            fragments.append(payload)
            if sum(map(len, fragments)) > MAX_WS_MESSAGE_BYTES:
                raise ConnectionError("WebSocket-Nachricht zu gross")
            if fin:
                return b"".join(fragments).decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self._send(0x8, struct.pack("!H", 1000))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class ArrError(Exception):
    def __init__(self, user_msg: str, *, status: int = 0, errors: list[tuple[str, str]] | None = None) -> None:
        super().__init__(user_msg)
        self.user_msg = user_msg
        self.status = status
        self.errors = errors or []

    @property
    def already_exists(self) -> bool:
        return any("exist" in code.lower() or "already" in msg.lower() for code, msg in self.errors)


def _arr_errors(detail: str) -> list[tuple[str, str]]:
    """Validierungsfehler aus einer Sonarr/Radarr-Fehlerantwort lesen."""
    try:
        data = json.loads(detail)
    except ValueError:
        return []
    if isinstance(data, dict):
        data = [data]
    return [(str(e.get("errorCode") or ""), str(e.get("errorMessage") or e.get("message") or ""))
            for e in _list(data) if isinstance(e, dict)]


_ARR_OPENER = _build_opener(redirects=False)


class ArrClient:
    def __init__(self, kind: str, url: str, api_key: str, quality_profile: str, root_folder: str) -> None:
        self.kind = kind                                   # "radarr" | "sonarr"
        self.name = "Radarr" if kind == "radarr" else "Sonarr"
        self.url, self.api_key = url, api_key
        self.quality_profile, self.root_folder = quality_profile, root_folder
        self._defaults: tuple[int, str, dict] | None = None
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.api_key)

    def call(self, method: str, path: str, body=None, timeout: float = 20):
        req = urllib.request.Request(
            f"{self.url}/api/v3{path}", method=method,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={"X-Api-Key": self.api_key, "Accept": "application/json",
                     "Content-Type": "application/json", "User-Agent": USER_AGENT})
        try:
            with _ARR_OPENER.open(req, timeout=timeout) as resp:
                raw = resp.read(MAX_ARR_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            detail = exc.read(20000).decode("utf-8", "replace")
            log.warning("%s %s %s -> HTTP %s: %s", self.name, method, path.split("?")[0], exc.code, _clean(detail, 500))
            if exc.code in (401, 403):
                raise ArrError(f"⚠️ {self.name} verweigert den Zugriff (API-Key falsch?) – bitte Admin Bescheid geben.",
                               status=exc.code)
            errors = _arr_errors(detail)
            reason = _clean(errors[0][1], 200) if errors and errors[0][1] else f"HTTP {exc.code}"
            raise ArrError(f"⚠️ {self.name} meldet einen Fehler: {reason}", status=exc.code, errors=errors)
        except (urllib.error.URLError, OSError) as exc:
            log.warning("%s nicht erreichbar (%s): %s", self.name, self.url, exc)
            raise ArrError(f"⚠️ {self.name} ist gerade nicht erreichbar. Bitte später nochmal versuchen.")
        if len(raw) > MAX_ARR_RESPONSE_BYTES:
            raise ArrError(f"⚠️ {self.name} hat zu viele Daten geliefert.")
        try:
            return json.loads(raw) if raw.strip() else None
        except ValueError:
            log.warning("%s: Antwort ist kein JSON – stimmt %s_URL?", self.name, self.kind.upper())
            raise ArrError(f"⚠️ {self.name} antwortet unerwartet – bitte Admin Bescheid geben.")

    def _get_defaults(self) -> tuple[int, str, dict]:
        """Qualitaetsprofil, Root-Folder (und bei Sonarr v3 Sprachprofil) einmalig ermitteln."""
        with self._lock:
            if self._defaults:
                return self._defaults
            config_error = ArrError(f"⚠️ {self.name} ist nicht richtig eingerichtet – bitte Admin Bescheid geben.")
            profiles = [p for p in _list(self.call("GET", "/qualityprofile")) if isinstance(p, dict)]
            wanted = self.quality_profile.strip()
            if wanted:
                profile = next((p for p in profiles if str(p.get("id")) == wanted
                                or str(p.get("name", "")).casefold() == wanted.casefold()), None)
            else:
                profile = profiles[0] if profiles else None
            if not profile:
                log.error("%s: Qualitaetsprofil %r nicht gefunden. Vorhanden: %s", self.name, wanted,
                          ", ".join(f"{p.get('name')} (id {p.get('id')})" for p in profiles) or "keine")
                raise config_error
            folders = [f for f in _list(self.call("GET", "/rootfolder")) if isinstance(f, dict)]
            wanted_folder = self.root_folder.strip().rstrip("/\\")
            if wanted_folder:
                folder = next((f for f in folders if str(f.get("path", "")).rstrip("/\\") == wanted_folder), None)
            else:
                folder = folders[0] if folders else None
            if not folder:
                log.error("%s: Root-Folder %r nicht gefunden. Vorhanden: %s", self.name, wanted_folder,
                          ", ".join(str(f.get("path")) for f in folders) or "keine")
                raise config_error
            extra: dict = {}
            if self.kind == "sonarr":
                version = str(_dict(self.call("GET", "/system/status")).get("version", ""))
                if version.startswith("3."):   # Sonarr v3 braucht noch ein Sprachprofil
                    languages = _list(self.call("GET", "/languageprofile"))
                    if languages:
                        extra["languageProfileId"] = _dict(languages[0]).get("id")
            self._defaults = (int(profile["id"]), str(folder["path"]), extra)
            log.info("%s: nutze Qualitaetsprofil '%s' und Root-Folder '%s'", self.name, profile.get("name"), folder["path"])
            return self._defaults

    def lookup(self, term: str) -> list[dict]:
        path = "/movie/lookup" if self.kind == "radarr" else "/series/lookup"
        result = self.call("GET", f"{path}?term={urllib.parse.quote(term)}")
        return [r for r in _list(result) if isinstance(r, dict)]

    def add(self, item: dict, search: bool = True, seasons: set[int] | None = None) -> dict:
        """Titel hinzufuegen. search=False: nur ueberwachen, keine automatische Suche starten.
        seasons (nur Sonarr): genau diese Staffeln ueberwachen; None = SONARR_MONITOR."""
        profile_id, root_folder, extra = self._get_defaults()
        body = dict(item)
        body.update(qualityProfileId=profile_id, rootFolderPath=root_folder, monitored=True, **extra)
        if self.kind == "radarr":
            body["minimumAvailability"] = RADARR_MINIMUM_AVAILABILITY
            body["addOptions"] = {"searchForMovie": search, "monitor": "movieOnly"}
            return _dict(self.call("POST", "/movie", body))
        body["seasonFolder"] = True
        monitor = SONARR_MONITOR
        if seasons is not None:
            # Getestet mit Sonarr v4: Staffel-Flags + monitor "skip" -> genau diese Staffeln samt Folgen
            body["seasons"] = [dict(_dict(s), monitored=_to_int(_dict(s).get("seasonNumber")) in seasons)
                               for s in _list(item.get("seasons"))]
            monitor = "skip"
        body["addOptions"] = {"monitor": monitor, "searchForMissingEpisodes": search,
                              "searchForCutoffUnmetEpisodes": False}
        return _dict(self.call("POST", "/series", body))

    def movie(self, movie_id: int) -> dict:
        return _dict(self.call("GET", f"/movie/{movie_id}"))

    def monitor_movie(self, movie_id: int) -> None:
        """Vorhandenen, nicht ueberwachten Film wieder ueberwachen und suchen (getestet mit Radarr)."""
        movie = self.movie(movie_id)
        movie["monitored"] = True
        self.call("PUT", f"/movie/{movie_id}", movie)
        self.call("POST", "/command", {"name": "MoviesSearch", "movieIds": [movie_id]})

    def remove(self, item_id: int) -> None:
        """Nur fuer Titel, die der Bot im selben Befehl selbst (ohne Suche) hinzugefuegt hat. Loescht keine Dateien."""
        if self.kind == "radarr":
            self.call("DELETE", f"/movie/{item_id}?deleteFiles=false&addImportExclusion=false")
        else:
            self.call("DELETE", f"/series/{item_id}?deleteFiles=false&addImportListExclusion=false")

    def series(self, series_id: int) -> dict:
        return _dict(self.call("GET", f"/series/{series_id}"))

    def monitor_seasons(self, series_id: int, seasons: set[int]) -> None:
        """Bestehende Serie: Staffeln zusaetzlich ueberwachen und danach suchen lassen."""
        series = self.series(series_id)
        series["monitored"] = True
        series["seasons"] = [dict(_dict(s), monitored=bool(_dict(s).get("monitored"))
                                  or _to_int(_dict(s).get("seasonNumber")) in seasons)
                             for s in _list(series.get("seasons"))]
        self.call("PUT", f"/series/{series_id}", series)
        if len(seasons) == 1:
            self.call("POST", "/command", {"name": "SeasonSearch", "seriesId": series_id,
                                           "seasonNumber": next(iter(seasons))})
        else:
            self.call("POST", "/command", {"name": "SeriesSearch", "seriesId": series_id})

    def parse(self, release_name: str) -> dict:
        """Release-Namen von Radarr/Sonarr zerlegen lassen (erkennt Titel, Jahr, Staffel/Folge)."""
        return _dict(self.call("GET", f"/parse?title={urllib.parse.quote(release_name)}"))

    def releases(self, item_id: int, season: int | None = None, episode_id: int | None = None) -> list[dict]:
        """Interaktive Suche bei den Indexern – dieselbe Liste wie "Interactive Search" im UI."""
        if self.kind == "radarr":
            path = f"/release?movieId={item_id}"
        elif episode_id:
            path = f"/release?episodeId={episode_id}"
        else:
            path = f"/release?seriesId={item_id}&seasonNumber={season or 0}"
        return [r for r in _list(self.call("GET", path, timeout=RELEASE_SEARCH_TIMEOUT)) if isinstance(r, dict)]

    def grab(self, release: dict) -> None:
        self.call("POST", "/release", {"guid": release.get("guid"), "indexerId": release.get("indexerId")}, timeout=60)


@dataclass(eq=False)
class Candidate:
    client: ArrClient
    item: dict

    @property
    def is_movie(self) -> bool:
        return self.client.kind == "radarr"

    @property
    def title(self) -> str:
        return _plain(_clean(self.item.get("title"), 150)) or "?"

    @property
    def year(self) -> int | None:
        year = _to_int(self.item.get("year"))
        return year if year and year > 0 else None

    @property
    def key(self) -> str:
        return f"movie:{self.item.get('tmdbId')}" if self.is_movie else f"series:{self.item.get('tvdbId')}"

    @property
    def exists(self) -> bool:
        return (_to_int(self.item.get("id")) or 0) > 0

    def label(self, details: bool = False) -> str:
        text = f"{'🎬' if self.is_movie else '📺'} {self.title}" + (f" ({self.year})" if self.year else "")
        if details and not self.is_movie:
            seasons = [s for s in _list(self.item.get("seasons")) if _to_int(_dict(s).get("seasonNumber"))]
            if seasons:
                text += f" – {len(seasons)} Staffel{'n' if len(seasons) != 1 else ''}"
        return text

    def poster(self) -> Poster | None:
        poster = _arr_poster(self.item.get("images"))
        remote = self.item.get("remotePoster")
        if poster is None and isinstance(remote, str) and _url_allowed(remote):
            poster = Poster(remote)
        return poster


@dataclass
class Command:
    text: str
    number: str
    uuid: str
    name: str
    timestamp: int
    in_group: bool
    reply_to: str
    quote_timestamp: int = 0         # bei bearbeiteten Nachrichten: die urspruengliche Nachricht

    @property
    def sender(self) -> str:
        return self.uuid or self.number


USAGE = ("ℹ️ **So fragst du etwas an:**\n"
         "!request <Titel> – Film oder Serie; geladen wird passend zum Qualitätsprofil\n"
         "Bei Serien fragt der Bot nach der Staffel (0 = alle) – oder direkt: „!request Beispielserie s2“\n"
         "!request film <Titel> / !request serie <Titel> – nur Filme / nur Serien\n"
         "Tipp: Jahr anhängen, z. B. „!request Beispielfilm 2021“\n"
         "\n"
         "**Bestimmtes Release wählen:**\n"
         "!releases <Titel> – Release-Liste der Indexer (Serien: „!releases <Titel> s2“)\n"
         "!releases – Liste für deinen zuletzt gewählten Titel\n"
         "!grab <Nr> – Release aus der Liste laden\n"
         "!release <Release-Name> – genau dieses Release laden\n"
         "\n"
         "Tipp: Auf eine Liste des Bots kannst du auch einfach mit der Nummer antworten (Zitat).\n"
         "!id – schickt dir deine Signal-ID privat (für Admin-Einstellungen)")
_COMMANDS = ("!request", "!releases", "!release", "!grab", "!help", "!hilfe", "!id")
_LIST_HINT = re.compile(r"„(!request|!releases|!grab) <Nr>")   # steht in jeder Auswahlliste des Bots
_SEASON_SUFFIX = re.compile(r"\s+(?:s|staffel\s*)(\d{1,2})$", re.IGNORECASE)
_EPISODE_TAG = re.compile(r"\bS\d{1,2}(?:E\d{1,3})?\b", re.IGNORECASE)


_UMLAUTS = (("ae", "ä"), ("oe", "ö"), ("ue", "ü"), ("Ae", "Ä"), ("Oe", "Ö"), ("Ue", "Ü"))


def _with_umlauts(text: str) -> str:
    """Release-Namen schreiben Umlaute aus ("Verrueckte") – die Titelsuche findet aber nur "Verrückte"."""
    for plain, umlaut in _UMLAUTS:
        text = text.replace(plain, umlaut)
    return text


def _fold(text: str) -> str:
    """Vergleichsform: Umlaute ausgeschrieben, nur Kleinbuchstaben/Ziffern."""
    text = (text or "").lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    return _norm(text)


def _fmt_size(value) -> str:
    size = _to_int(value) or 0
    if size <= 0:
        return ""
    return f"{size / 1024 ** 3:.1f} GB" if size >= 1024 ** 3 else f"{size / 1024 ** 2:.0f} MB"


def _rejection(release: dict) -> str:
    reasons = _list(release.get("rejections"))
    if not reasons:
        return "vom Profil abgelehnt"
    first = reasons[0]
    return _clean(first.get("reason") if isinstance(first, dict) else first, 120) or "vom Profil abgelehnt"


def _release_lines(index: int, release: dict) -> str:
    quality = _dict(_dict(release.get("quality")).get("quality")).get("name")
    facts = [_clean(quality, 30), _fmt_size(release.get("size"))]
    if release.get("protocol") == "torrent" and release.get("seeders") is not None:
        facts.append(f"{_to_int(release.get('seeders')) or 0} Seeds")
    facts.append(_clean(release.get("indexer"), 30))
    text = f"{index}. {_plain(_clean(release.get('title'), 100))}\n    " + _plain(" · ".join(f for f in facts if f))
    if not release.get("approved"):
        text += f"\n    ⛔ {_plain(_rejection(release))}"
    return text


def _release_hint(item: dict) -> str:
    """Hinweis fuer Filme, die noch nicht erschienen sind (Radarr laedt dann erst spaeter)."""
    status = str(item.get("status") or "").lower()
    if status not in ("announced", "incinemas", "tba"):
        return ""
    today = time.strftime("%Y-%m-%d")
    dates = sorted(str(item.get(k))[:10] for k in ("digitalRelease", "physicalRelease")
                   if item.get(k) and str(item.get(k))[:10] >= today)
    when = f" (voraussichtlich ab {dates[0][8:10]}.{dates[0][5:7]}.{dates[0][:4]})" if dates else ""
    return f"\n📅 Noch nicht als Download erschienen{when} – wird automatisch geladen, sobald es verfügbar ist."


_RELEASE_JUNK = re.compile(r"\.(mkv|mp4|avi|m4v|ts|wmv|nzb|torrent)$", re.IGNORECASE)


def _clean_release_name(text: str) -> str:
    """Eingefuegte Release-Namen aufraeumen: Anfuehrungszeichen/Backticks/<>, Dateiendung."""
    text = _clean(text, 250).strip().strip("\"'`„“”‚‘’<>«»").strip()
    return _RELEASE_JUNK.sub("", text)

_KIND_WORDS = {"film": "radarr", "filme": "radarr", "movie": "radarr",
               "serie": "sonarr", "serien": "sonarr", "series": "sonarr", "show": "sonarr", "tv": "sonarr"}
_TRAILING_YEAR = re.compile(r"^(.*\S)\s+\(?((?:19|20)\d{2})\)?$")


class RequestService:
    def __init__(self, relay: Relay) -> None:
        self.relay = relay
        self.clients = [c for c in (
            ArrClient("radarr", RADARR_URL, RADARR_API_KEY, RADARR_QUALITY_PROFILE, RADARR_ROOT_FOLDER),
            ArrClient("sonarr", SONARR_URL, SONARR_API_KEY, SONARR_QUALITY_PROFILE, SONARR_ROOT_FOLDER),
        ) if c.enabled]
        self.group_id = _internal_group_id(SIGNAL_RECIPIENT)
        self._commands: "queue.Queue[Command]" = queue.Queue(maxsize=100)
        self._pending: dict[str, tuple[float, list[Candidate]]] = {}
        self._adds: dict[str, list[tuple[float, str]]] = {}   # (Zeit, Titel-Key) fuer das Limit
        self._recent: dict[str, list[float]] = {}
        self._warned: dict[str, float] = {}
        self._last_title: dict[str, tuple[float, Candidate]] = {}                 # fuer "!releases" ohne Titel
        self._season_lists: dict[str, tuple[float, Candidate, list[int]]] = {}    # Staffelfrage bei Serien
        self._release_lists: dict[str, tuple[float, Candidate, list[dict]]] = {}  # fuer "!grab <Nr>"
        self._members: tuple[float, frozenset[str]] = (0.0, frozenset())

    def start(self) -> None:
        for target, name in ((self._receiver_loop, "signal-receiver"), (self._worker_loop, "request-worker"),
                             (self._check_clients, "arr-check")):
            threading.Thread(target=target, name=name, daemon=True).start()
        log.info("!request aktiv fuer: %s", ", ".join(c.name for c in self.clients))

    def _check_clients(self) -> None:
        for client in self.clients:
            try:
                status = _dict(client.call("GET", "/system/status"))
                log.info("%s erreichbar (Version %s)", client.name, status.get("version", "?"))
            except ArrError as exc:
                log.warning("%s beim Start nicht nutzbar: %s", client.name, exc.user_msg)

    # -- Empfang ------------------------------------------------------------ #
    def _receive_url(self) -> str:
        parts = urllib.parse.urlsplit(SIGNAL_API_URL)
        scheme = "wss" if parts.scheme == "https" else "ws"
        number = urllib.parse.quote(SIGNAL_NUMBER, safe="+")
        return f"{scheme}://{parts.netloc}{parts.path.rstrip('/')}/v1/receive/{number}"

    def _receiver_loop(self) -> None:
        backoff = 2
        while True:
            ws = None
            try:
                ws = WebSocketClient(self._receive_url(), timeout=30)
                log.info("Signal-Empfang verbunden – warte auf !request")
                backoff = 2
                while True:
                    raw = ws.recv()
                    if raw is None:
                        raise ConnectionError("vom Server geschlossen")
                    self.on_raw_message(raw)
            except Exception as exc:  # noqa: BLE001 - Empfang muss weiterlaufen
                log.warning("Signal-Empfang unterbrochen (%s) – neuer Versuch in %d s", exc, backoff)
            finally:
                if ws:
                    ws.close()
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

    def on_raw_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except ValueError:
            return
        cmd = self.parse(_dict(data))
        if cmd is None:
            return
        # Direktnachrichten von Nicht-Mitgliedern schon hier verwerfen: sonst bekaemen Fremde
        # z. B. die Spam-Bremsen-Antwort und koennten die Befehls-Queue fuellen.
        if not cmd.in_group and not {i for i in (cmd.number, cmd.uuid) if i} & self._group_members():
            log.info("Direktnachricht von Nicht-Mitglied ignoriert (%s)", _mask_number(cmd.sender))
            return
        if not self._throttle(cmd):
            return
        try:
            self._commands.put_nowait(cmd)
        except queue.Full:
            log.warning("Zu viele offene Anfragen – Befehl verworfen")

    def parse(self, data: dict) -> Command | None:
        """Signal-Envelope -> Command (oder None, wenn es kein fuer uns relevanter Befehl ist)."""
        envelope = _dict(data.get("envelope") or _dict(data.get("params")).get("envelope"))
        # Formate (mitgeschnitten von signal-cli-rest-api 0.101, json-rpc):
        #   andere Mitglieder:        envelope.dataMessage
        #   ... bearbeitet:           envelope.editMessage.dataMessage
        #   eigenes Konto (Handy):    envelope.syncMessage.sentMessage
        #   ... bearbeitet:           envelope.syncMessage.sentMessage.editMessage.dataMessage
        edit = _dict(envelope.get("editMessage"))
        message = _dict(envelope.get("dataMessage")) or _dict(edit.get("dataMessage"))
        own = False
        if not message:   # eigene Nachricht von einem anderen Geraet desselben Kontos
            sent = _dict(_dict(envelope.get("syncMessage")).get("sentMessage"))
            edit = _dict(sent.get("editMessage"))
            message = _dict(edit.get("dataMessage")) or sent
            own = bool(message)
        # Bei Bearbeitungen die urspruengliche Nachricht zitieren (die zeigt Signal an)
        quote_ts = _to_int(edit.get("targetSentTimestamp")) or 0
        text = message.get("message")
        if not isinstance(text, str):
            return None
        # @-Erwaehnungen stehen als Platzhalterzeichen (U+FFFC) im Text, z. B. "@Bot !request ..."
        text = text.replace("￼", " ").strip()
        first_word = text.split(None, 1)[0].lower() if text else ""
        if first_word not in _COMMANDS:
            # Antwort per Zitat auf eine Auswahlliste des Bots: einfach "2" statt "!request 2"
            quote = _dict(message.get("quote"))
            quoted_bot = SIGNAL_NUMBER in (quote.get("authorNumber"), quote.get("author"))
            hint = _LIST_HINT.search(str(quote.get("text") or ""))
            if not (text.isdigit() and quoted_bot and hint):
                return None
            text = f"{hint.group(1)} {text}"

        timestamp = _to_int(message.get("timestamp") or envelope.get("timestamp")) or 0
        if timestamp and time.time() - timestamp / 1000 > REQUEST_MAX_AGE_SECONDS:
            log.info("Veralteten Befehl ignoriert")
            return None
        number = str(envelope.get("sourceNumber") or envelope.get("source") or "")
        uuid = str(envelope.get("sourceUuid") or "")
        if not (number or uuid):
            return None

        group_id = _dict(message.get("groupInfo")).get("groupId") or _dict(message.get("groupV2")).get("id")
        if group_id:
            if not self.group_id or group_id != self.group_id:
                return None                                  # andere Gruppe
            in_group, reply_to = True, SIGNAL_RECIPIENT
        else:
            if own or not REQUEST_VIA_DM:
                return None                                  # eigene DMs an Dritte / DMs abgeschaltet
            in_group, reply_to = False, number or uuid
        return Command(text=_clean(text, 300), number=number, uuid=uuid,
                       name=_plain(_clean(envelope.get("sourceName"), 60)) or "jemand",
                       timestamp=timestamp, in_group=in_group, reply_to=reply_to,
                       quote_timestamp=quote_ts or timestamp)

    def _throttle(self, cmd: Command) -> bool:
        now = time.time()
        recent = [t for t in self._recent.get(cmd.sender, []) if now - t < 60]
        if len(recent) >= COMMANDS_PER_MINUTE:
            log.warning("Spam-Bremse: Befehl von %s ignoriert", _mask_number(cmd.sender))
            if now - self._warned.get(cmd.sender, 0) > 60:     # nur einmal pro Minute hinweisen
                self._warned[cmd.sender] = now
                self._reply(cmd, "🐢 Zu viele Befehle auf einmal – bitte eine Minute warten.")
            return False
        recent.append(now)
        self._recent[cmd.sender] = recent
        return True

    # -- Verarbeitung --------------------------------------------------------- #
    def _prune(self) -> None:
        """Abgelaufene Auswahllisten und alte Zaehler wegraeumen (sonst waechst der Speicher langsam mit)."""
        now = time.time()
        # eine Stunde Gnadenfrist, damit "Liste abgelaufen" noch gemeldet werden kann
        for store in (self._pending, self._last_title, self._season_lists, self._release_lists):
            for key in [k for k, v in store.items() if v[0] < now - 3600]:
                del store[key]
        for store in (self._recent,):
            for key in [k for k, v in store.items() if not v or now - v[-1] > 60]:
                del store[key]
        for key in [k for k, v in self._adds.items() if not v or now - v[-1][0] > 86400]:
            del self._adds[key]
        for key in [k for k, t in self._warned.items() if now - t > 60]:
            del self._warned[key]

    def _worker_loop(self) -> None:
        while True:
            cmd = self._commands.get()
            try:
                self._prune()
                self.handle(cmd)
            except Exception:  # noqa: BLE001
                log.exception("Fehler bei der Verarbeitung von %r", cmd.text)
                self._reply(cmd, "⚠️ Da ist intern etwas schiefgelaufen – bitte später nochmal versuchen.")

    def _reply(self, cmd: Command, text: str, poster: Poster | None = None) -> None:
        author = cmd.number or cmd.uuid
        quote_ts = cmd.quote_timestamp or cmd.timestamp
        quote = ({"quote_timestamp": quote_ts, "quote_author": author, "quote_message": cmd.text}
                 if quote_ts and author else None)
        self.relay.send(text, recipient=cmd.reply_to, quote=quote, poster=poster)

    def _group_members(self) -> frozenset[str]:
        now = time.time()
        fetched_at, members = self._members
        if now - fetched_at < 600:
            return members
        if not self.group_id:
            members = frozenset({SIGNAL_RECIPIENT})
        else:
            url = (f"{SIGNAL_API_URL}/v1/groups/{urllib.parse.quote(SIGNAL_NUMBER, safe='+')}/"
                   f"{urllib.parse.quote(SIGNAL_RECIPIENT, safe='=')}")
            try:
                with _SIGNAL_OPENER.open(urllib.request.Request(url), timeout=15) as resp:
                    data = _dict(json.loads(resp.read(MAX_BODY_BYTES)))
                members = frozenset(str(m) for m in _list(data.get("members")))
            except Exception as exc:  # noqa: BLE001
                log.warning("Gruppenmitglieder nicht abrufbar: %s", exc)
                return members
        self._members = (now, members)
        return members

    def _is_admin(self, cmd: Command) -> bool:
        return bool({i for i in (cmd.number, cmd.uuid) if i} & REQUEST_ADMINS)

    def _authorized(self, cmd: Command) -> bool:
        ids = {i for i in (cmd.number, cmd.uuid) if i}
        if REQUEST_ALLOWED_SENDERS and not ids & (REQUEST_ALLOWED_SENDERS | REQUEST_ADMINS):
            self._reply(cmd, "⛔ Du bist für Anfragen nicht freigeschaltet.")
            return False
        if not cmd.in_group and not ids & self._group_members():
            log.info("Direktnachricht von Nicht-Mitglied ignoriert (%s)", _mask_number(cmd.sender))
            return False
        return True

    def handle(self, cmd: Command) -> None:
        if not self._authorized(cmd):
            return
        word, _, args = cmd.text.partition(" ")
        word, args = word.lower(), args.strip()
        if word in ("!help", "!hilfe") or (word == "!request" and not args):
            self._reply(cmd, USAGE)
        elif word == "!id":
            self._cmd_id(cmd)
        elif word == "!request":
            if args.isdigit() and self._season_choice(cmd, int(args)):
                return
            if not args.isdigit():
                self._season_lists.pop(cmd.sender, None)   # neue Suche: alte Staffelfrage gilt nicht mehr
            season = None
            match = _SEASON_SUFFIX.search(" " + args)
            if match:
                season, args = int(match.group(1)), (" " + args)[:match.start()].strip()
            cand = self._resolve(cmd, args, "!request", f" s{season}" if season is not None else "")
            if cand:
                self._add(cmd, cand, season)
        elif word == "!releases":
            self._cmd_releases(cmd, args)
        elif word == "!grab":
            self._cmd_grab(cmd, args)
        elif word == "!release":
            self._cmd_release(cmd, args)

    def _cmd_id(self, cmd: Command) -> None:
        """Eigene Signal-IDs privat schicken – noetig fuer REQUEST_ADMINS/REQUEST_ALLOWED_SENDERS,
        vor allem bei Leuten, die ihre Telefonnummer in Signal verbergen (dann zaehlt nur die UUID)."""
        lines = ["🆔 **Deine Signal-IDs** (für REQUEST_ADMINS / REQUEST_ALLOWED_SENDERS):"]
        if cmd.number:
            lines.append(f"Nummer: {cmd.number}")
        if cmd.uuid:
            lines.append(f"UUID: {cmd.uuid}")
        if cmd.in_group:
            self._reply(cmd, "📬 Ich habe dir deine ID privat geschickt.")
        self.relay.send("\n".join(lines), recipient=cmd.number or cmd.uuid)

    # -- Titel finden ----------------------------------------------------------- #
    def _resolve(self, cmd: Command, args: str, action: str, hint: str = "") -> Candidate | None:
        """Titel per Listennummer oder Suche bestimmen. Bei mehreren Treffern: Liste senden, None liefern."""
        if args.isdigit():
            choice = int(args)
            expires, options = self._pending.get(cmd.sender, (0.0, []))
            if 1 <= choice <= len(options):
                if time.time() > expires:
                    self._pending.pop(cmd.sender, None)
                    self._reply(cmd, "⌛ Deine Trefferliste ist abgelaufen – bitte nochmal suchen.")
                    return None
                self._pending.pop(cmd.sender, None)
                return options[choice - 1]

        query, wanted = args, None
        first, _, rest = args.partition(" ")
        if first.lower() in _KIND_WORDS and rest.strip():
            wanted, query = _KIND_WORDS[first.lower()], rest
        query = _clean(query, 100)
        if len(query) < 2:
            self._reply(cmd, f"❓ Bitte einen Titel angeben, z. B. „{action} Beispielfilm“.")
            return None
        clients = [c for c in self.clients if wanted in (None, c.kind)]
        if not clients:
            self._reply(cmd, f"⚠️ {'Filme' if wanted == 'radarr' else 'Serien'} können hier nicht angefragt werden.")
            return None

        title, year = query, None
        match = _TRAILING_YEAR.match(query)
        if match:
            title, year = match.group(1), int(match.group(2))

        candidates: list[Candidate] = []
        notes: list[str] = []
        for client in clients:
            try:
                items = client.lookup(query)
                if not items and year:
                    # z. B. Sonarr findet "Titel 2022" nicht, "Titel" aber schon -> Jahr danach pruefen
                    items = client.lookup(title)
                candidates += [Candidate(client, item) for item in items[:10]]
            except ArrError as exc:
                notes.append(exc.user_msg)
        if not candidates:
            text = "\n".join(notes) if notes else f"🔍 Nichts gefunden für „{_plain(query)}“."
            if _EPISODE_TAG.search(query) or query.count(".") >= 3:   # sieht aus wie ein Release-Name
                text += f"\nTipp: Für einen Release-Namen nimm „!release {_plain(query)}“."
            self._reply(cmd, text)
            return None
        exact = [c for c in candidates if _norm(c.title) == _norm(title) and (year is None or c.year == year)]
        if len(exact) == 1 and not notes:
            return exact[0]

        per_kind = 4 if len(clients) > 1 else 8
        shown, counts = [], {}
        for cand in exact + [c for c in candidates if c not in exact]:
            if counts.get(cand.client.kind, 0) < per_kind and len(shown) < 8:
                shown.append(cand)
                counts[cand.client.kind] = counts.get(cand.client.kind, 0) + 1
        self._pending[cmd.sender] = (time.time() + SELECTION_TTL_SECONDS, shown)
        self._season_lists.pop(cmd.sender, None)      # neue Trefferliste ersetzt eine offene Staffelfrage
        lines = [f"🔍 **Treffer für „{_plain(query)}“:**"]
        lines += [f"{i}. {c.label(details=True)}" + (" – schon vorhanden" if c.exists else "")
                  for i, c in enumerate(shown, 1)]
        lines += ["", f"Antworte mit „{action} <Nr>{hint}“ (gilt {SELECTION_TTL_SECONDS // 60} Min.)"]
        lines += notes
        self._reply(cmd, "\n".join(lines))
        return None

    # -- Limit (zaehlt pro Titel, Admins unbegrenzt) ---------------------------- #
    def _limit_reached(self, cmd: Command, key: str) -> bool:
        if REQUEST_LIMIT_PER_DAY <= 0 or self._is_admin(cmd):
            return False
        now = time.time()
        history = [(t, k) for t, k in self._adds.get(cmd.sender, []) if now - t < 86400]
        self._adds[cmd.sender] = history
        keys = {k for _, k in history}
        if key in keys or len(keys) < REQUEST_LIMIT_PER_DAY:
            return False
        self._reply(cmd, f"⛔ Du hast in den letzten 24 Stunden schon {len(keys)} Titel angefragt "
                         f"(Limit {REQUEST_LIMIT_PER_DAY}). Bitte später nochmal.")
        return True

    def _count(self, cmd: Command, cand: Candidate) -> None:
        self._adds.setdefault(cmd.sender, []).append((time.time(), cand.key))
        self.relay.note_requester(cand.key, cmd.name)

    # -- !request ---------------------------------------------------------------- #
    # -- Serien: Staffel waehlen ------------------------------------------------- #
    def _season_overview(self, cand: Candidate) -> tuple[list[int], dict[int, dict]]:
        """Staffelnummern (ohne Specials) und – bei vorhandenen Serien – Stand je Staffel."""
        seasons = cand.item.get("seasons")
        if cand.exists:
            try:
                seasons = cand.client.series(_to_int(cand.item.get("id")) or 0).get("seasons")
            except ArrError:
                pass   # dann eben ohne Detailstand
        info = {}
        for season in _list(seasons):
            season = _dict(season)
            number = _to_int(season.get("seasonNumber"))
            if number and number > 0:
                info[number] = season
        return sorted(info), info

    @staticmethod
    def _season_complete(season: dict) -> bool:
        stats = _dict(season.get("statistics"))
        total = _to_int(stats.get("totalEpisodeCount")) or 0
        return total > 0 and (_to_int(stats.get("episodeFileCount")) or 0) >= total

    def _ask_season(self, cmd: Command, cand: Candidate, numbers: list[int], info: dict[int, dict]) -> None:
        self._season_lists[cmd.sender] = (time.time() + SELECTION_TTL_SECONDS, cand, numbers)
        lines = [f"📺 **{cand.label()}** – welche Staffel?", "0. Alle Staffeln"]
        for n in numbers:
            season = info.get(n, {})
            total = _to_int(_dict(season.get("statistics")).get("totalEpisodeCount"))
            line = f"{n}. Staffel {n}" + (f" ({total} Folgen)" if total else "")
            if self._season_complete(season):
                line += " ✔️ vorhanden"
            elif cand.exists and season.get("monitored"):
                line += " ⏳ angefragt"
            lines.append(line)
        lines += ["", f"Antworte mit „!request <Nr>“ (gilt {SELECTION_TTL_SECONDS // 60} Min.)"]
        self._reply(cmd, "\n".join(lines), poster=cand.poster())

    def _season_choice(self, cmd: Command, choice: int) -> bool:
        """'!request <Nr>' nach einer Staffelfrage. False = keine passende Staffelfrage offen."""
        expires, cand, numbers = self._season_lists.get(cmd.sender, (0.0, None, []))
        if cand is None or (choice != 0 and choice not in numbers):
            return False
        self._season_lists.pop(cmd.sender, None)
        if time.time() > expires:
            self._reply(cmd, "⌛ Die Staffelauswahl ist abgelaufen – bitte nochmal „!request <Titel>“.")
            return True
        self._add_series(cmd, cand, set(numbers) if choice == 0 else {choice}, numbers)
        return True

    def _add_series(self, cmd: Command, cand: Candidate, chosen: set[int], numbers: list[int]) -> None:
        if not chosen or (len(chosen) > 1 and chosen == set(numbers)):
            what = "alle Staffeln"
        elif len(chosen) == 1:
            what = f"Staffel {next(iter(chosen))}"
        else:
            what = "Staffeln " + ", ".join(map(str, sorted(chosen)))
        if cand.exists:
            _, info = self._season_overview(cand)
            if chosen and all(self._season_complete(info.get(n, {})) for n in chosen):
                self._reply(cmd, f"{cand.label()} – {what} ist schon vollständig da 🍿")
                return
            if self._limit_reached(cmd, cand.key):
                return
            try:
                cand.client.monitor_seasons(_to_int(cand.item.get("id")) or 0, chosen)
            except ArrError as exc:
                self._reply(cmd, exc.user_msg)
                return
        else:
            if self._limit_reached(cmd, cand.key):
                return
            try:
                # keine Staffeln bekannt (z. B. noch nicht angelaufen) -> Voreinstellung SONARR_MONITOR
                cand.item = dict(cand.item, **cand.client.add(cand.item, search=True, seasons=chosen or None))
            except ArrError as exc:
                if exc.already_exists:
                    self._reply(cmd, f"{cand.label()} ist schon in {cand.client.name} vorhanden 📚")
                else:
                    self._reply(cmd, exc.user_msg)
                return
        self._count(cmd, cand)
        log.info("Angefragt von %s: %s (%s) -> Sonarr", _mask_number(cmd.sender), cand.label(), what)
        where = "hier" if cmd.in_group else "in der Gruppe"
        upcoming = ("\n📅 Die Serie ist noch nicht gestartet – Folgen werden geladen, sobald sie erscheinen."
                    if str(cand.item.get("status") or "").lower() == "upcoming" else "")
        self._reply(cmd, f"✅ **Angefragt:** {cand.label()} – {what}\n"
                         f"Sonarr sucht jetzt passend zum Qualitätsprofil – neue Folgen werden {where} angekündigt."
                         f"{upcoming}", poster=cand.poster())

    def _add(self, cmd: Command, cand: Candidate, season: int | None = None) -> None:
        self._last_title[cmd.sender] = (time.time() + 3600, cand)
        if not cand.is_movie:
            numbers, info = self._season_overview(cand)
            if season is not None:
                if season not in numbers:
                    known = ", ".join(map(str, numbers)) or "keine"
                    self._reply(cmd, f"❓ Staffel {season} gibt es bei {cand.label()} nicht. Verfügbar: {known}.")
                    return
                self._add_series(cmd, cand, {season}, numbers)
            elif len(numbers) > 1:
                self._ask_season(cmd, cand, numbers, info)
            else:
                self._add_series(cmd, cand, set(numbers), numbers)   # nur eine Staffel -> keine Rueckfrage
            return
        where = "hier" if cmd.in_group else "in der Gruppe"
        if cand.exists:
            # Die Suche liefert hasFile/monitored bei vorhandenen Filmen nicht zuverlaessig -> direkt fragen
            try:
                movie = cand.client.movie(_to_int(cand.item.get("id")) or 0)
            except ArrError as exc:
                self._reply(cmd, exc.user_msg)
                return
            if movie.get("hasFile"):
                self._reply(cmd, f"{cand.label()} ist schon verfügbar 🍿")
                return
            if movie.get("monitored"):
                self._reply(cmd, f"{cand.label()} ist schon angefragt und wird gesucht ⏳{_release_hint(movie)}")
                return
            # vorhanden, aber nicht ueberwacht -> Radarr wuerde ihn nie suchen: wieder aktivieren
            if self._limit_reached(cmd, cand.key):
                return
            try:
                cand.client.monitor_movie(_to_int(movie.get("id")) or 0)
            except ArrError as exc:
                self._reply(cmd, exc.user_msg)
                return
            self._count(cmd, cand)
            log.info("Wieder ueberwacht von %s: %s", _mask_number(cmd.sender), cand.label())
            self._reply(cmd, f"✅ **Angefragt:** {cand.label()}\nWar in Radarr, wurde aber nicht gesucht – "
                             f"jetzt wieder aktiv. Sobald es da ist, wird es {where} angekündigt."
                             f"{_release_hint(movie)}", poster=cand.poster())
            return
        if self._limit_reached(cmd, cand.key):
            return
        try:
            cand.item = dict(cand.item, **cand.client.add(cand.item))
        except ArrError as exc:
            if exc.already_exists:
                self._reply(cmd, f"{cand.label()} ist schon in {cand.client.name} vorhanden 📚")
            else:
                self._reply(cmd, exc.user_msg)
            return
        self._count(cmd, cand)
        log.info("Angefragt von %s: %s -> %s", _mask_number(cmd.sender), cand.label(), cand.client.name)
        self._reply(cmd, f"✅ **Angefragt:** {cand.label()}\n"
                         f"{cand.client.name} sucht jetzt danach – sobald es da ist, wird es {where} angekündigt."
                         f"{_release_hint(cand.item)}\n"
                         f"Lieber ein bestimmtes Release? → „!releases“",
                    poster=cand.poster())

    def _ensure_in_library(self, cmd: Command, cand: Candidate) -> Candidate | None:
        """Fuer die Release-Auswahl muss der Titel in Radarr/Sonarr sein – sonst ohne Auto-Suche hinzufuegen."""
        if cand.exists:
            return cand
        if self._limit_reached(cmd, cand.key):
            return None
        try:
            cand.item = dict(cand.item, **cand.client.add(cand.item, search=False))
        except ArrError as exc:
            self._reply(cmd, exc.user_msg)
            return None
        self._count(cmd, cand)
        log.info("Ohne Auto-Suche hinzugefuegt von %s: %s", _mask_number(cmd.sender), cand.label())
        self._reply(cmd, f"➕ {cand.label()} wurde zu {cand.client.name} hinzugefügt (ohne automatische Suche).")
        return cand

    # -- !releases --------------------------------------------------------------- #
    def _cmd_releases(self, cmd: Command, args: str) -> None:
        season = None
        match = _SEASON_SUFFIX.search(" " + args)
        if match:
            season, args = int(match.group(1)), (" " + args)[:match.start()].strip()
        season_hint = f" s{season}" if season is not None else ""
        if args:
            cand = self._resolve(cmd, args, "!releases", season_hint)
        else:
            expires, cand = self._last_title.get(cmd.sender, (0.0, None))
            if not cand or time.time() > expires:
                self._reply(cmd, "❓ Für welchen Titel? Z. B. „!releases Beispielfilm 2021“ oder „!releases Beispielserie s2“.")
                return
        if not cand:
            return
        self._last_title[cmd.sender] = (time.time() + 3600, cand)
        if not cand.is_movie and season is None:
            seasons = sorted(n for n in (_to_int(_dict(s).get("seasonNumber")) for s in _list(cand.item.get("seasons")))
                             if n)
            known = f" Verfügbar: {', '.join(map(str, seasons))}." if seasons else ""
            self._reply(cmd, f"❓ Welche Staffel von {cand.label()}? Z. B. „!releases s1“.{known}")
            return
        cand = self._ensure_in_library(cmd, cand)
        if cand:
            self._show_releases(cmd, cand, season)

    def _search_releases(self, cmd: Command, cand: Candidate, season: int | None,
                         episode_id: int | None = None) -> list[dict] | None:
        what = cand.label() + (f" – Staffel {season}" if season is not None and not episode_id else "")
        self._reply(cmd, f"🔎 Suche Releases für {what} bei den Indexern … (kann bis zu einer Minute dauern)")
        item_id = _to_int(cand.item.get("id"))
        if not item_id:
            self._reply(cmd, f"⚠️ {cand.label()} ist in {cand.client.name} nicht auffindbar.")
            return None
        try:
            return cand.client.releases(item_id, season, episode_id)
        except ArrError as exc:
            self._reply(cmd, exc.user_msg)
            return None

    def _show_releases(self, cmd: Command, cand: Candidate, season: int | None) -> None:
        releases = self._search_releases(cmd, cand, season)
        if releases is None:
            return
        admin = self._is_admin(cmd)
        visible = [r for r in releases if admin or r.get("approved")]
        if not visible:
            hidden = len(releases) - len(visible)
            extra = f" ({hidden} passen nicht zum Qualitätsprofil – die darf nur ein Admin holen)" if hidden else ""
            self._reply(cmd, f"📭 Keine passenden Releases für {cand.label()} gefunden{extra}.")
            return
        shown = visible[:max(RELEASES_SHOWN, 1)]
        self._release_lists[cmd.sender] = (time.time() + SELECTION_TTL_SECONDS, cand, shown)
        lines = [f"📦 **Releases für {cand.label()}**" + (f" – Staffel {season}" if season is not None else "") + ":"]
        lines += [_release_lines(i, r) for i, r in enumerate(shown, 1)]
        more = len(visible) - len(shown)
        lines += ["", f"Antworte mit „!grab <Nr>“ (gilt {SELECTION_TTL_SECONDS // 60} Min.)"
                  + (f" · {more} weitere nicht angezeigt" if more > 0 else "")]
        self._reply(cmd, "\n".join(lines))

    # -- !grab ------------------------------------------------------------------- #
    def _cmd_grab(self, cmd: Command, args: str) -> None:
        if not args.isdigit():
            self._reply(cmd, "❓ Nutzung: „!grab <Nr>“ – die Nummer aus deiner letzten „!releases“-Liste.")
            return
        expires, cand, releases = self._release_lists.get(cmd.sender, (0.0, None, []))
        choice = int(args)
        if not releases:
            self._reply(cmd, "❓ Du hast keine offene Release-Liste – erst „!releases <Titel>“.")
            return
        if time.time() > expires:
            self._release_lists.pop(cmd.sender, None)
            self._reply(cmd, "⌛ Deine Release-Liste ist abgelaufen – bitte „!releases“ nochmal ausführen.")
            return
        if not 1 <= choice <= len(releases):
            self._reply(cmd, f"❓ Bitte eine Nummer von 1 bis {len(releases)} wählen.")
            return
        self._grab(cmd, cand, releases[choice - 1])

    def _grab(self, cmd: Command, cand: Candidate, release: dict) -> bool:
        title = _plain(_clean(release.get("title"), 120))
        if not release.get("approved") and not self._is_admin(cmd):
            self._reply(cmd, f"⛔ „{title}“ passt nicht zum Qualitätsprofil ({_plain(_rejection(release))}). "
                             f"Das darf nur ein Admin laden.")
            return False
        if self._limit_reached(cmd, cand.key):
            return False
        try:
            cand.client.grab(release)
        except ArrError as exc:
            if exc.status == 404:
                self._reply(cmd, "⌛ Das Release ist nicht mehr im Zwischenspeicher von "
                                 f"{cand.client.name} – bitte „!releases“ nochmal ausführen.")
            else:
                self._reply(cmd, exc.user_msg)
            return False
        self._count(cmd, cand)
        self._release_lists.pop(cmd.sender, None)
        log.info("Release geladen von %s: %s (%s)", _mask_number(cmd.sender), title, cand.label())
        where = "hier" if cmd.in_group else "in der Gruppe"
        override = "" if release.get("approved") else "\n⚠️ Admin-Override: passt eigentlich nicht zum Profil."
        self._reply(cmd, f"⬇️ **Wird geladen:** {title}\nfür {cand.label()} – sobald es fertig ist, "
                         f"wird es {where} angekündigt.{override}")
        return True

    # -- !release <Release-Name> ------------------------------------------------- #
    def _cmd_release(self, cmd: Command, args: str) -> None:
        name = _clean_release_name(args)
        if len(name) < 5:
            self._reply(cmd, "❓ Nutzung: „!release <exakter Release-Name>“, "
                             "z. B. „!release Beispielfilm.2024.2160p.WEB-DL.DDP5.1-GROUP“.")
            return
        # Serien-Release erkennbar an S01 / S01E02 – sonst zuerst als Film versuchen. Versteht Radarr den
        # Namen nicht (kein Jahr), probieren wir Sonarr (z. B. Serien-Packs ohne Staffel-Tag).
        order = ("sonarr", "radarr") if _EPISODE_TAG.search(name) else ("radarr", "sonarr")
        parsed, client, kind = {}, None, ""
        for candidate_kind in order:
            arr = next((c for c in self.clients if c.kind == candidate_kind), None)
            if arr is None:
                continue
            try:
                result = arr.parse(name)
            except ArrError as exc:
                self._reply(cmd, exc.user_msg)
                return
            info = _dict(result.get("parsedMovieInfo" if candidate_kind == "radarr" else "parsedEpisodeInfo"))
            usable = (result.get("movie") or result.get("series")
                      or (info.get("year") if candidate_kind == "radarr" else info.get("seriesTitle")))
            if client is None or usable:
                parsed, client, kind = result, arr, candidate_kind
            if usable:
                break
        if client is None:
            self._reply(cmd, "⚠️ Filme/Serien können hier nicht angefragt werden.")
            return
        info = _dict(parsed.get("parsedMovieInfo") if kind == "radarr" else parsed.get("parsedEpisodeInfo"))
        known = _dict(parsed.get("movie") if kind == "radarr" else parsed.get("series"))

        season, episode = None, None
        if kind == "sonarr":
            season = _to_int(info.get("seasonNumber"))
            if season is None:
                self._reply(cmd, "❓ Im Release-Namen fehlt die Staffel (z. B. S02). Bei Anime mit durchgehender "
                                 "Folgennummer bitte „!releases <Titel> s<Staffel>“ nutzen.")
                return
            numbers = _list(info.get("episodeNumbers"))
            if not info.get("fullSeason") and numbers:
                episode = _to_int(numbers[0])

        if known:
            candidates = [Candidate(client, known)]
        else:
            if kind == "radarr":
                titles = _list(info.get("movieTitles")) or [info.get("movieTitle")]
                title = _clean(titles[0] if titles else "", 100)
                year = _to_int(info.get("year")) or None
            else:
                title, year = _clean(info.get("seriesTitle"), 100), None
            if not title:
                self._reply(cmd, "❓ Den Release-Namen konnte ich nicht lesen – stimmt die Schreibweise?")
                return
            try:
                candidates = self._release_candidates(client, title, year)
            except ArrError as exc:
                self._reply(cmd, exc.user_msg)
                return
            if not candidates:
                self._reply(cmd, f"🔍 Zu „{_plain(title)}“{f' ({year})' if year else ''} finde ich in {client.name} "
                                 f"keinen Film/keine Serie.")
                return

        self._reply(cmd, f"🔎 Suche „{_plain(name)}“ bei den Indexern … (kann etwas dauern)")
        tried: list[str] = []
        for cand in candidates:
            added_now = False
            if not cand.exists:
                if self._limit_reached(cmd, cand.key):
                    return
                try:
                    cand.item = dict(cand.item, **client.add(cand.item, search=False))
                    added_now = True
                except ArrError as exc:
                    self._reply(cmd, exc.user_msg)
                    return
            item_id = _to_int(cand.item.get("id")) or 0
            episode_id = self._episode_id(client, name, season, episode) if episode is not None else None
            try:
                releases = client.releases(item_id, season, episode_id)
            except ArrError as exc:
                self._rollback(cand, added_now)
                self._reply(cmd, exc.user_msg)
                return
            target = _norm(name)
            titles = [(_norm(str(r.get("title", ""))), r) for r in releases]
            release = next((r for t, r in titles if t == target), None)
            if release is None:
                # Manche Indexer haengen etwas an ("-xpost", "[eztv]") -> eindeutiger Praefix-Treffer zaehlt
                prefixed = [r for t, r in titles if t.startswith(target) and len(t) - len(target) <= 15]
                release = prefixed[0] if len(prefixed) == 1 else None
            if release is not None:
                self._last_title[cmd.sender] = (time.time() + 3600, cand)
                if added_now:
                    log.info("Fuer !release ohne Auto-Suche hinzugefuegt: %s", cand.label())
                if not self._grab(cmd, cand, release):
                    self._rollback(cand, added_now)
                return
            # Nicht exakt da, aber sehr aehnliche Releases (Tippfehler, andere Gruppe/Qualitaet)?
            # Dann ist es mit hoher Wahrscheinlichkeit der richtige Titel -> Auswahl anbieten.
            scored = sorted(((difflib.SequenceMatcher(None, target, t).ratio(), r) for t, r in titles),
                            key=lambda pair: pair[0], reverse=True)
            close = [r for score, r in scored if score >= 0.8 and (self._is_admin(cmd) or r.get("approved"))][:5]
            if close:
                self._last_title[cmd.sender] = (time.time() + 3600, cand)
                self._release_lists[cmd.sender] = (time.time() + SELECTION_TTL_SECONDS, cand, close)
                lines = [f"🔍 „{_plain(name)}“ gibt es so nicht – ähnliche Releases für {cand.label()}:"]
                lines += [_release_lines(i, r) for i, r in enumerate(close, 1)]
                lines += ["", f"Antworte mit „!grab <Nr>“ (gilt {SELECTION_TTL_SECONDS // 60} Min.)"]
                self._reply(cmd, "\n".join(lines))
                return
            tried.append(cand.label())
            self._rollback(cand, added_now)   # falscher Titel -> wieder raus, naechsten Kandidaten pruefen

        checked = f" (geprüft: {', '.join(tried)})" if tried else ""
        self._reply(cmd, f"🔍 „{_plain(name)}“ haben die Indexer nicht (mehr){checked}.")

    def _release_candidates(self, client: ArrClient, title: str, year: int | None) -> list[Candidate]:
        """Moegliche Titel zu einem Release-Namen: mit/ohne Jahr, mit wiederhergestellten Umlauten.
        Die eigentliche Pruefung passiert danach: nur wenn es GENAU dieses Release gibt, wird geladen."""
        terms: list[str] = []
        for variant in (title, _with_umlauts(title)):
            for term in ((f"{variant} {year}",) if year else ()) + (variant,):
                if term not in terms:
                    terms.append(term)
        hits: list[Candidate] = []
        for term in terms:
            hits = [Candidate(client, item) for item in client.lookup(term)]
            if hits:
                break

        def rank(cand: Candidate) -> tuple[int, int]:
            names = [cand.title, str(cand.item.get("originalTitle") or "")]
            names += [str(_dict(a).get("title") or "") for a in _list(cand.item.get("alternateTitles"))]
            title_match = any(_fold(n) == _fold(title) for n in names if n)
            return (0 if title_match else 1, 0 if year is None or cand.year == year else 1)

        return sorted(hits, key=rank)[:3]   # sorted() ist stabil: sonst gilt die Reihenfolge der Suche

    def _rollback(self, cand: Candidate, added_now: bool) -> None:
        if not added_now:
            return
        try:
            cand.client.remove(_to_int(cand.item.get("id")) or 0)
            log.info("Wieder entfernt (Release nicht gefunden): %s", cand.label())
        except ArrError as exc:
            log.warning("Konnte %s nicht wieder entfernen: %s", cand.label(), exc.user_msg)

    @staticmethod
    def _episode_id(client: ArrClient, name: str, season: int, number: int | None) -> int | None:
        """Episoden-ID fuer eine Einzelfolge; bei frisch hinzugefuegten Serien kurz auf Sonarr warten."""
        for _ in range(6):
            try:
                episodes = _list(client.parse(name).get("episodes"))
            except ArrError:
                return None
            for ep in episodes:
                ep = _dict(ep)
                if _to_int(ep.get("seasonNumber")) == season and _to_int(ep.get("episodeNumber")) == number:
                    return _to_int(ep.get("id"))
            time.sleep(2)
        return None


# --------------------------------------------------------------------------- #
# HTTP-Server
# --------------------------------------------------------------------------- #


def _parse_body(raw: bytes, content_type: str = "") -> dict:  # noqa: ARG001 - Header nur fuers Log relevant
    """JSON – oder Formulardaten. Jellyfins Webhook-Plugin hat zwei fast gleich benannte Ziele:
    "Generic" schickt JSON, "Generic Form" schickt key=value&... (auch wenn der Header JSON behauptet)."""
    text = raw.decode("utf-8").lstrip("﻿ \t\r\n")
    # Am Inhalt entscheiden, nicht am Header – "Generic Form" behauptet ggf. application/json.
    if text.startswith(("{", "[")):
        data = json.loads(text)
    else:
        fields = urllib.parse.parse_qs(text, keep_blank_values=True, strict_parsing=True)
        data = {key: values[-1] for key, values in fields.items()}
    if not isinstance(data, dict) or not data:
        raise ValueError("JSON-Objekt oder Formulardaten erwartet")
    return data


class Handler(BaseHTTPRequestHandler):
    server_version = "SignalMediaRelay"
    sys_version = ""
    timeout = REQUEST_TIMEOUT_SECONDS  # Socket-Timeout gegen haengende/langsame Clients

    def log_message(self, fmt, *args):  # Standard-Accesslog nur im Debug, Token geschwaerzt
        log.debug("%s - %s", self.address_string(), _redact(fmt % args))

    def _reply(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _supplied_tokens(self, query: dict) -> list[str]:
        tokens = []
        header = self.headers.get("X-Relay-Token")
        if header:
            tokens.append(header.strip())
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(auth[6:].strip(), validate=True).decode("utf-8")
                tokens.append(decoded.partition(":")[2])  # Passwort-Teil
            except (binascii.Error, UnicodeDecodeError):
                pass
        tokens.extend(query.get("token") or [])
        return tokens

    def _authorized(self, query: dict) -> bool:
        if not RELAY_TOKEN:
            return ALLOW_INSECURE_NO_TOKEN
        expected = RELAY_TOKEN.encode()
        return any(hmac.compare_digest(t.encode(), expected) for t in self._supplied_tokens(query))

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path.rstrip("/")
        if path == "/health":
            self._reply(200, {"status": "ok", "version": __version__})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self):
        parts = urllib.parse.urlsplit(self.path)
        path = parts.path.rstrip("/")
        handler = ROUTES.get(path)
        if handler is None:
            self._reply(404, {"error": "not found"})
            return
        if not self._authorized(urllib.parse.parse_qs(parts.query)):
            log.warning("Abgelehnt (falsches Token) von %s auf %s", self.address_string(), path)
            self._reply(401, {"error": "unauthorized"})
            return

        length = _to_int(self.headers.get("Content-Length")) or 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self._reply(400, {"error": "invalid body size"})
            return
        raw = self.rfile.read(length)
        try:
            data = _parse_body(raw, self.headers.get("Content-Type") or "")
        except (ValueError, UnicodeDecodeError) as exc:
            # Anfang des Inhalts mitloggen (bereinigt/gekuerzt) – z. B. kaputtes Jellyfin-Template erkennen
            log.warning("Ungueltiges JSON auf %s (%s, Content-Type %r): %s", path, exc,
                        self.headers.get("Content-Type"), _clean(raw[:200].decode("utf-8", "replace"), 200))
            self._reply(400, {"error": "invalid json"})
            return

        log.debug("Payload %s: %s", path, json.dumps(data)[:2000])
        try:
            result = handler(data)
        except Exception:  # noqa: BLE001
            log.exception("Fehler beim Verarbeiten von %s", path)
            self._reply(500, {"error": "internal error"})
            return
        log.debug("%s -> %s", path, result)
        self._reply(200, {"result": result})


_RADARR_AVAILABILITY = {"announced", "inCinemas", "released"}
_SONARR_MONITOR_TYPES = {"all", "future", "missing", "existing", "firstSeason", "lastSeason", "latestSeason",
                         "pilot", "recent", "monitorSpecials", "unmonitorSpecials", "none"}


def validate_config() -> list[str]:
    """Tippfehler in der Konfiguration beim Start melden statt erst bei der ersten Anfrage im Chat."""
    problems = []
    if not re.fullmatch(r"\+\d{6,15}", SIGNAL_NUMBER or ""):
        problems.append("SIGNAL_NUMBER muss im Format +491701234567 sein")
    if not (SIGNAL_RECIPIENT.startswith("group.") and _internal_group_id(SIGNAL_RECIPIENT)) \
            and not re.fullmatch(r"\+\d{6,15}", SIGNAL_RECIPIENT):
        problems.append("SIGNAL_RECIPIENT muss eine gültige Gruppen-ID (group.…) oder Telefonnummer sein")
    for name, url in (("SIGNAL_API_URL", SIGNAL_API_URL), ("JELLYFIN_URL", JELLYFIN_URL),
                      ("RADARR_URL", RADARR_URL), ("SONARR_URL", SONARR_URL)):
        if url and urllib.parse.urlsplit(url).scheme not in ("http", "https"):
            problems.append(f"{name} muss mit http:// oder https:// beginnen")
    if LOG_LEVEL not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        problems.append("LOG_LEVEL muss DEBUG, INFO, WARNING oder ERROR sein")
    for name, value, low, high in (("LISTEN_PORT", LISTEN_PORT, 1, 65535),
                                   ("AGGREGATE_SECONDS", AGGREGATE_SECONDS, 0, 3600),
                                   ("RELEASES_SHOWN", RELEASES_SHOWN, 1, 20),
                                   ("RELEASE_SEARCH_TIMEOUT", RELEASE_SEARCH_TIMEOUT, 10, 900),
                                   ("REQUEST_COMMANDS_PER_MINUTE", COMMANDS_PER_MINUTE, 1, 60)):
        if not low <= value <= high:
            problems.append(f"{name} muss zwischen {low} und {high} liegen (ist {value})")
    if REQUESTS_ENABLED:
        if RADARR_MINIMUM_AVAILABILITY not in _RADARR_AVAILABILITY:
            problems.append(f"RADARR_MINIMUM_AVAILABILITY muss eins von {sorted(_RADARR_AVAILABILITY)} sein")
        if SONARR_MONITOR not in _SONARR_MONITOR_TYPES:
            problems.append(f"SONARR_MONITOR muss eins von {sorted(_SONARR_MONITOR_TYPES)} sein")
        if (RADARR_URL and not RADARR_API_KEY) or (RADARR_API_KEY and not RADARR_URL):
            problems.append("RADARR_URL und RADARR_API_KEY nur zusammen setzen")
        if (SONARR_URL and not SONARR_API_KEY) or (SONARR_API_KEY and not SONARR_URL):
            problems.append("SONARR_URL und SONARR_API_KEY nur zusammen setzen")
    return problems


def main() -> None:
    global RELAY
    missing = [n for n, v in (("SIGNAL_NUMBER", SIGNAL_NUMBER), ("SIGNAL_RECIPIENT", SIGNAL_RECIPIENT)) if not v]
    if missing:
        sys.exit(f"Konfigurationsfehler: fehlende Variablen: {', '.join(missing)}")
    problems = validate_config()
    if problems:
        sys.exit("Konfigurationsfehler:\n  - " + "\n  - ".join(problems))
    if not RELAY_TOKEN:
        if not ALLOW_INSECURE_NO_TOKEN:
            sys.exit("Konfigurationsfehler: RELAY_TOKEN fehlt "
                     "(zum bewussten Abschalten: ALLOW_INSECURE_NO_TOKEN=true)")
        log.warning("RELAY_TOKEN ist leer – Webhooks werden ohne Authentifizierung angenommen!")
    elif RELAY_TOKEN in ("change-me", "changeme") or len(RELAY_TOKEN) < 16:
        sys.exit("Konfigurationsfehler: RELAY_TOKEN ist zu schwach (mind. 16 Zeichen, nicht 'change-me')")

    def _on_sigterm(*_):  # Docker stoppt per SIGTERM -> sauber herunterfahren
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _on_sigterm)

    if REQUESTS_ENABLED:
        if not ((RADARR_URL and RADARR_API_KEY) or (SONARR_URL and SONARR_API_KEY)):
            sys.exit("Konfigurationsfehler: REQUESTS_ENABLED=true, aber weder RADARR_URL+RADARR_API_KEY "
                     "noch SONARR_URL+SONARR_API_KEY gesetzt")
        for name, url in (("RADARR_URL", RADARR_URL), ("SONARR_URL", SONARR_URL)):
            if url and urllib.parse.urlsplit(url).scheme not in ("http", "https"):
                sys.exit(f"Konfigurationsfehler: {name} muss mit http:// oder https:// beginnen")
        if not SIGNAL_RECIPIENT.startswith("group.") and not REQUEST_ALLOWED_SENDERS:
            log.warning("SIGNAL_RECIPIENT ist keine Gruppe – !request wird nur per Direktnachricht vom Empfaenger angenommen")

    RELAY = Relay()
    if REQUESTS_ENABLED:
        RequestService(RELAY).start()
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    log.info("Signal Media Relay %s lauscht auf %s:%d -> %s (Empfaenger %s)",
             __version__, LISTEN_HOST, LISTEN_PORT, SIGNAL_API_URL, _mask_number(SIGNAL_RECIPIENT))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        log.info("Beende – sende ausstehende Ankuendigungen …")
        RELAY.flush_all()
        RELAY.wait_until_sent(timeout=20)
        server.server_close()


if __name__ == "__main__":
    main()
