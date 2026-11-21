#!/usr/bin/env python3
"""
End-to-end test for the !request feature – no Docker, no Signal account, no real Sonarr/Radarr.

A fake server plays:
  * signal-cli-rest-api: WebSocket /v1/receive/<number> (pushes chat messages to the relay),
    POST /v2/send (records replies), GET /v1/groups/... (member list for DM checks)
  * Radarr + Sonarr:     /api/v3/{movie,series}/lookup, qualityprofile, rootfolder, system/status, POST add

relay.py runs as a real subprocess. The test "types" commands into the chat and checks the replies.

Run:  python tests/test_requests.py      (exit code 0 = all checks passed)
"""

import base64
import hashlib
import json
import os
import queue
import re
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RELAY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "relay.py")
TOKEN = "s3cret-token-0123456789"
BOT = "+4900000000"
GROUP_INTERNAL = "dGVzdC1ncm91cC1pZA=="                      # internal id as it appears in messages
GROUP = "group." + base64.b64encode(GROUP_INTERNAL.encode()).decode()
MEMBER, MEMBER2, OUTSIDER = "+4911111111", "+4912121212", "+4999999999"
MEMBER3, ADMIN = "+4913131313", "+4914141414"
MEMBER4, MEMBER5, MEMBER6 = "+4915151515", "+4916161616", "+4917171717"
MEMBER7, MEMBER8, MEMBER9 = "+4918181818", "+4919191919", "+4920202020"
MEMBER10 = "+4921212121"

chat_in: "queue.Queue[dict | None]" = queue.Queue()         # messages pushed to the relay via WebSocket
sent: list[dict] = []                                       # what the relay sent to Signal
added: list[tuple[str, dict]] = []                          # what the relay added to Radarr/Sonarr
grabs: list[tuple[str, dict]] = []                          # releases the relay told Radarr/Sonarr to download
library: dict[int, dict] = {7: {"kind": "radarr", "title": "Existing", "year": 2020, "tmdbId": 5,
                                "hasFile": True, "monitored": True},
                            8: {"kind": "radarr", "title": "Forgotten", "year": 2019, "tmdbId": 8009,
                                "hasFile": False, "monitored": False}}
GB = 1024 ** 3


def dotted(title: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", ".", title).strip(".")


def fake_releases(query: dict) -> list[dict]:
    """Indexer results as Radarr/Sonarr return them from GET /release."""
    def rel(title, approved, size, quality, protocol="torrent", seeders=50, reason=""):
        return {"guid": f"guid-{title}", "indexerId": 3, "indexer": "TestIndexer", "title": title,
                "size": size, "quality": {"quality": {"name": quality}}, "approved": approved,
                "rejections": [] if approved else [reason], "protocol": protocol, "seeders": seeders}
    if "movieId" in query:
        movie = library[int(query["movieId"][0])]
        base = f"{dotted(movie['title'])}.{movie['year']}"
        if movie["title"] == "Green Meadow":
            return [rel("Gruene.Wiese.1985.German.1080p.BluRay.x264-GRP", True, 9 * GB, "Bluray-1080p")]
        return [rel(f"{base}.2160p.UHD.BluRay.x265-TERMiNAL", True, 30 * GB, "Bluray-2160p"),
                rel(f"{base}.1080p.BluRay.x264-SPARKS", True, 12 * GB, "Bluray-1080p", "usenet", None),
                rel(f"{base}.720p.HDTV.x264-LOL", False, 2 * GB, "HDTV-720p",
                    reason="HDTV-720p is not wanted in profile")]
    if "episodeId" in query:
        return [rel("Sample.Show.Origins.S01E02.1080p.WEB-DL-NTb", True, 3 * GB, "WEBDL-1080p")]
    series = library[int(query["seriesId"][0])]
    season = int(query["seasonNumber"][0])
    if series["title"] == "The Big Coup":
        return [rel("Der.Grosse.Coup.S01.German.DL.1080p.WEB.x264-NTb", True, 25 * GB, "WEBDL-1080p")]
    base = f"{dotted(series['title'])}.S{season:02d}"
    return [rel(f"{base}.1080p.WEB-DL-NTb", True, 20 * GB, "WEBDL-1080p"),
            rel(f"{base}.480p.WEB-XYZ", False, 4 * GB, "WEBDL-480p", reason="WEBDL-480p is not wanted in profile")]


def fake_parse(kind: str, name: str) -> dict:
    """Minimal stand-in for GET /api/v3/parse."""
    if kind == "radarr":
        m = re.match(r"^(.+?)\.((?:19|20)\d{2})\.", name)
        if not m:
            return {"title": name}
        title, year = m.group(1).replace(".", " "), int(m.group(2))
        movie = next((dict(v, id=k) for k, v in library.items()
                      if v["kind"] == "radarr" and dotted(v["title"]).lower() == dotted(title).lower()), None)
        return {"title": name, "parsedMovieInfo": {"movieTitles": [title], "year": year}, "movie": movie}
    m = re.match(r"^(.+?)\.S(\d+)(?:E(\d+))?\.", name, re.IGNORECASE)
    if not m:
        return {"title": name}
    title, season, ep = m.group(1).replace(".", " "), int(m.group(2)), m.group(3)
    series = next((dict(v, id=k) for k, v in library.items()
                   if v["kind"] == "sonarr" and dotted(v["title"]).lower() == dotted(title).lower()), None)
    episodes = [{"id": 500 + int(ep), "seasonNumber": season, "episodeNumber": int(ep)}] if series and ep else []
    return {"title": name, "series": series, "episodes": episodes,
            "parsedEpisodeInfo": {"seriesTitle": title, "seasonNumber": season, "fullSeason": ep is None,
                                  "episodeNumbers": [int(ep)] if ep else []}}


def in_library(kind: str, items: list[dict]) -> list[dict]:
    """Lookup results carry an id once the title is in the library (like the real API)."""
    out = []
    for item in items:
        key = "tmdbId" if kind == "radarr" else "tvdbId"
        lib_id = next((k for k, v in library.items() if v["kind"] == kind and v.get(key) == item.get(key)), None)
        out.append(dict(item, id=lib_id) if lib_id else dict(item))
    return out
ws_connected = threading.Event()
failures: list[str] = []

RADARR_LOOKUP = {
    "test movie": [{"title": "Test Movie", "year": 2016, "tmdbId": 8001,
                 "images": [{"coverType": "poster", "remoteUrl": "http://127.0.0.1:1/blocked.jpg"}]},
                {"title": "The Test Movie", "year": 1996, "tmdbId": 8002}],
    "sample movie": [{"title": "Sample Movie", "year": 2021, "tmdbId": 8003},
             {"title": "Sample Movie", "year": 1984, "tmdbId": 8004},
             {"title": "Sample Movie: Part Two", "year": 2024, "tmdbId": 8005}],
    "existing": [{"title": "Existing", "year": 2020, "tmdbId": 5, "id": 7, "hasFile": True}],
    "exists": [{"title": "Exists", "year": 2019, "tmdbId": 999}],
    "space movie": [{"title": "Space Movie", "year": 2014, "tmdbId": 8006}],
    "space movie 2014": [{"title": "Space Movie", "year": 2014, "tmdbId": 8006}],
    "unknown movie 2020": [],
    "forgotten": [{"title": "Forgotten", "year": 2019, "tmdbId": 8009}],
    # 0.9.1: limit bypass via the "similar releases" branch of !release
    "typo movie 2015": [{"title": "Typo Movie", "year": 2015, "tmdbId": 8012}],
    "count movie a": [{"title": "Count Movie A", "year": 2016, "tmdbId": 8013}],
    "count movie b": [{"title": "Count Movie B", "year": 2017, "tmdbId": 8014}],
    "future movie": [{"title": "Future Movie", "year": 2099, "tmdbId": 8011, "status": "announced",
                      "digitalRelease": "2099-05-01T00:00:00Z"}],
    # German release names: only the umlaut spelling finds the movie, under its original title
    "grüne wiese 1985": [{"title": "Green Meadow", "year": 1985, "tmdbId": 8007}],
}
ORIGIN_SHOW = {"title": "Sample Show: Origins", "year": 2024, "tvdbId": 9002,
            "seasons": [{"seasonNumber": 0}, {"seasonNumber": 1}]}
SAMPLE_SHOW = {"title": "Sample Show", "year": 2022, "tvdbId": 9001,
             "seasons": [{"seasonNumber": 0, "monitored": False}, {"seasonNumber": 1, "monitored": False},
                         {"seasonNumber": 2, "monitored": False}]}
SONARR_LOOKUP = {"sample movie": [ORIGIN_SHOW], "sample show origins": [ORIGIN_SHOW], "sample show": [SAMPLE_SHOW],
                 # first hit is the wrong show -> relay must roll it back and try the next one
                 "der grosse coup": [{"title": "Coup Show", "year": 2018, "tvdbId": 9003,
                                      "seasons": [{"seasonNumber": 1}]},
                                     {"title": "The Big Coup", "year": 2017, "tvdbId": 9004,
                                      "seasons": [{"seasonNumber": 1}, {"seasonNumber": 2}]}]}
deletes: list[str] = []
# Episode statistics the fake Sonarr reports per season once a show is in the library
SEASON_STATS = {("Sample Show", 1): (9, 9), ("Sample Show", 2): (0, 10)}   # (files, total)
puts: list[dict] = []
commands: list[dict] = []


def check(name: str, ok) -> None:
    print(("PASS  " if ok else "FAIL  ") + name, flush=True)
    if not ok:
        failures.append(name)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, code: int, body) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _arr_auth(self) -> str | None:
        kind = "radarr" if self.path.startswith("/radarr") else "sonarr"
        return kind if self.headers.get("X-Api-Key") == f"{kind}key" else None

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path)
        if path.path.startswith("/v1/receive/"):
            return self._websocket()
        if path.path.startswith("/v1/groups/"):
            return self._json(200, {"id": GROUP, "members": [BOT, MEMBER, MEMBER2]})
        kind = self._arr_auth()
        if kind is None:
            return self._json(401, {"message": "Unauthorized"})
        sub = path.path.split("/api/v3", 1)[1]
        term = urllib.parse.parse_qs(path.query).get("term", [""])[0].lower()
        if sub == "/system/status":
            return self._json(200, {"version": "5.9.0" if kind == "radarr" else "4.0.10"})
        if sub == "/qualityprofile":
            return self._json(200, [{"id": 4, "name": "HD-1080p"}, {"id": 6, "name": "Ultra-HD"}])
        if sub == "/rootfolder":
            return self._json(200, [{"id": 1, "path": "/movies" if kind == "radarr" else "/tv"}])
        if sub == "/movie/lookup":
            return self._json(200, in_library("radarr", RADARR_LOOKUP.get(term, [])))
        if sub == "/series/lookup":
            if term == "kaputt":
                return self._json(500, {"message": "Database is locked"})
            return self._json(200, in_library("sonarr", SONARR_LOOKUP.get(term, [])))
        m = re.fullmatch(r"/movie/(\d+)", sub)
        if m and int(m.group(1)) in library:
            return self._json(200, dict(library[int(m.group(1))], id=int(m.group(1))))
        m = re.fullmatch(r"/series/(\d+)", sub)
        if m and int(m.group(1)) in library:
            show = library[int(m.group(1))]
            seasons = []
            for s in show.get("seasons", []):
                files, total = SEASON_STATS.get((show["title"], s["seasonNumber"]), (0, 1))
                seasons.append(dict(s, statistics={"episodeFileCount": files, "totalEpisodeCount": total}))
            return self._json(200, dict(show, id=int(m.group(1)), seasons=seasons))
        if sub == "/parse":
            return self._json(200, fake_parse(kind, urllib.parse.parse_qs(path.query)["title"][0]))
        if sub == "/release":
            return self._json(200, fake_releases(urllib.parse.parse_qs(path.query)))
        self._json(404, {})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"null")
        if self.path == "/v2/send":
            sent.append(body)
            return self._json(201, {"timestamp": "1"})
        kind = self._arr_auth()
        if kind is None:
            return self._json(401, {"message": "Unauthorized"})
        if self.path.endswith("/api/v3/command"):
            commands.append(body)
            return self._json(201, dict(body, status="queued"))
        if self.path.endswith("/api/v3/release"):
            if not str(body.get("guid", "")).startswith("guid-"):
                return self._json(404, {"message": "Couldn't find requested release in cache"})
            grabs.append((kind, body))
            return self._json(200, {})
        if body.get("tmdbId") == 999:
            return self._json(400, [{"propertyName": "TmdbId", "errorCode": "MovieExistsValidator",
                                     "errorMessage": "This movie has already been added"}])
        added.append((kind, body))
        new_id = 100 + len(library)
        library[new_id] = {"kind": kind, "title": body["title"], "year": body.get("year"),
                           "tmdbId": body.get("tmdbId"), "tvdbId": body.get("tvdbId"),
                           "seasons": body.get("seasons", [])}
        self._json(201, dict(body, id=new_id))

    def do_DELETE(self):
        if self._arr_auth() is None:
            return self._json(401, {"message": "Unauthorized"})
        deletes.append(self.path)
        library.pop(int(urllib.parse.urlsplit(self.path).path.rsplit("/", 1)[1]), None)
        self._json(200, {})

    def do_PUT(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"null")
        if self._arr_auth() is None:
            return self._json(401, {"message": "Unauthorized"})
        puts.append(body)
        item_id = int(self.path.rsplit("/", 1)[1])
        if "seasons" in body:
            library[item_id]["seasons"] = [{k: v for k, v in s.items() if k != "statistics"} for s in body["seasons"]]
        library[item_id]["monitored"] = body.get("monitored")
        self._json(202, body)

    def _websocket(self):
        key = self.headers["Sec-WebSocket-Key"]
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept.decode())
        self.end_headers()
        self.wfile.flush()
        ws_connected.set()
        while True:
            event = chat_in.get()
            if event is None:
                return
            data = json.dumps(event).encode()
            n = len(data)
            head = bytes([0x81]) + (bytes([n]) if n < 126 else bytes([126]) + struct.pack("!H", n))
            self.wfile.write(head + data)
            self.wfile.flush()


_last_ts = [0]


def r_ts_hint() -> int:
    """Timestamp of the last simulated message."""
    return _last_ts[0]


def say(text: str, *, sender: str = MEMBER, name: str = "Anna", group: str | None = GROUP_INTERNAL,
        age: float = 0, own: bool = False, edit: bool = False, quote: dict | None = None) -> None:
    """Simulate a Signal message arriving at the bot."""
    time.sleep(0.3)               # let late replies to the previous command arrive ...
    for msg in sent:              # ... and only match replies to THIS command afterwards
        msg["_seen"] = True
    ts = int((time.time() - age) * 1000)
    _last_ts[0] = ts
    message = {"timestamp": ts, "message": text}
    if quote:
        message["quote"] = quote
    if group:
        message["groupInfo"] = {"groupId": group, "type": "DELIVER"}
    envelope = {"source": sender, "sourceNumber": sender, "sourceUuid": f"uuid-{sender}",
                "sourceName": name, "timestamp": ts}
    if own and edit:   # format as captured live (signal-cli-rest-api 0.101): own account, edited
        envelope["syncMessage"] = {"sentMessage": {"destination": None, "destinationNumber": None,
                                                   "editMessage": {"targetSentTimestamp": ts - 5000,
                                                                   "dataMessage": message}}}
    elif own:
        envelope["syncMessage"] = {"sentMessage": dict(message, destination=None)}
    elif edit:
        envelope["editMessage"] = {"targetSentTimestamp": ts - 5000, "dataMessage": message}
    else:
        envelope["dataMessage"] = message
    chat_in.put({"envelope": envelope, "account": BOT})


def expect_reply(contains: str, timeout: float = 10) -> dict | None:
    """Wait for the next reply that contains the given text."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for msg in sent:
            if contains in msg["message"] and not msg.get("_seen"):
                msg["_seen"] = True
                return msg
        time.sleep(0.1)
    return None


def no_new_reply(wait: float = 2.0) -> bool:
    before = len(sent)
    time.sleep(wait)
    return len(sent) == before


def main() -> int:
    fake_port, relay_port = free_port(), free_port()
    fake = f"http://127.0.0.1:{fake_port}"
    server = ThreadingHTTPServer(("127.0.0.1", fake_port), Fake)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()

    env = dict(os.environ, SIGNAL_API_URL=fake, SIGNAL_NUMBER=BOT, SIGNAL_RECIPIENT=GROUP,
               RELAY_TOKEN=TOKEN, LISTEN_HOST="127.0.0.1", LISTEN_PORT=str(relay_port),
               AGGREGATE_SECONDS="1", SEND_POSTERS="false", REQUESTS_ENABLED="true",
               RADARR_URL=f"{fake}/radarr", RADARR_API_KEY="radarrkey", RADARR_QUALITY_PROFILE="Ultra-HD",
               SONARR_URL=f"{fake}/sonarr", SONARR_API_KEY="sonarrkey",
               REQUEST_LIMIT_PER_DAY="2", REQUEST_ADMINS=ADMIN, PYTHONIOENCODING="utf-8")

    # Startup refuses incomplete request configuration
    p = subprocess.run([sys.executable, RELAY], capture_output=True, text=True, timeout=15,
                       env=dict(env, RADARR_URL="", SONARR_URL=""))
    check("refuses to start with REQUESTS_ENABLED but no Radarr/Sonarr", p.returncode != 0)

    log_path = os.path.join(tempfile.gettempdir(), "signal-media-relay-requests.log")
    log_file = open(log_path, "w", encoding="utf-8")
    relay = subprocess.Popen([sys.executable, RELAY], env=env, stdout=log_file, stderr=log_file)
    try:
        check("relay connects to the Signal WebSocket", ws_connected.wait(15))

        say("!request")
        r = expect_reply("How to request")
        check("'!request' alone -> usage help, quoted", r and r.get("quote_author") == MEMBER
              and r["recipients"] == [GROUP])

        say("!request Test Movie")
        r = expect_reply("Requested:")
        check("single exact match -> added directly, success reply", r and "Test Movie (2016)" in r["message"])
        movie = next((b for k, b in added if k == "radarr" and b.get("tmdbId") == 8001), None)
        check("Radarr add uses configured profile, root folder, search on",
              movie and movie["qualityProfileId"] == 6 and movie["rootFolderPath"] == "/movies"
              and movie["addOptions"]["searchForMovie"] is True)

        say("!request NichtsDa", sender=MEMBER2, name="Chris")
        check("no results -> 'Nothing found'", expect_reply("Nothing found") is not None)

        say("!request film Existing", sender=MEMBER2, name="Chris")
        check("already downloaded -> 'already available'", expect_reply("already available") is not None)

        say("!request Exists", sender=MEMBER2, name="Chris")
        check("Radarr 'already added' error -> friendly reply", expect_reply("already in Radarr") is not None)

        say("!request serie Kaputt", sender=MEMBER2, name="Chris")
        check("Sonarr server error -> error reported back", expect_reply("Sonarr reports an error") is not None)

        say("!request Sample Movie")
        r = expect_reply("Results for")
        check("ambiguous -> numbered list with movies and series",
              r and "1. 🎬 Sample Movie (2021)" in r["message"] and "📺 Sample Show: Origins (2024) – 1 season" in r["message"])
        say("!request 2")
        expect_reply("Requested:")
        check("'!request 2' adds the 2nd entry (Sample Movie 1984)", any(b.get("tmdbId") == 8004 for _, b in added))

        say("!request Space Movie")
        check("daily limit (2) reached -> refused", expect_reply("limit 2") is not None)
        check("limit: nothing added to Radarr", not any(b.get("tmdbId") == 8006 for _, b in added))

        say("!request Sample Movie", sender="+4922222222", name="Ben", group="b3RoZXItZ3JvdXA=")
        check("message from another group -> ignored", no_new_reply())

        say("!request Sample Movie", age=7200)
        check("message older than 1 h -> ignored", no_new_reply())

        say("!request", sender=MEMBER2, group=None)
        r = expect_reply("How to request")
        check("DM from group member -> answered via DM", r and r["recipients"] == [MEMBER2])

        say("!request Sample Movie", sender=OUTSIDER, group=None)
        check("DM from non-member -> ignored", no_new_reply())

        say("!request", sender=BOT, name="Bot-Owner", own=True)
        check("own message from phone (sync message) -> handled", expect_reply("How to request") is not None)

        for _ in range(7):
            say("!hilfe", sender="+4933333333", name="Spam")
        time.sleep(3)
        spam = [m for m in sent if m.get("quote_author") == "+4933333333"]
        check("spam brake: 6 answered + 1 'slow down' notice",
              len(spam) == 7 and sum("Too many commands" in m["message"] for m in spam) == 1)

        # ---------------- shows: season question ----------------
        dora = dict(sender=MEMBER4, name="Dora")
        say("!request Sample Show", **dora)
        r = expect_reply("which season")
        check("show with several seasons -> asks: 0 = all, 1, 2 (no specials)",
              r and "0. All seasons" in r["message"] and "1. Season 1" in r["message"]
              and "2. Season 2" in r["message"] and "Season 0" not in r["message"])
        check("season question: nothing added yet", not any(b.get("tvdbId") == 9001 for _, b in added))
        say("!request 2", **dora)
        r = expect_reply("Requested:")
        sev = next((b for k, b in added if b.get("tvdbId") == 9001), None)
        flags = {s["seasonNumber"]: s["monitored"] for s in sev["seasons"]} if sev else {}
        check("'!request 2' -> only season 2 monitored (monitor=skip), search started",
              r and "Sample Show (2022) – season 2" in r["message"] and flags == {0: False, 1: False, 2: True}
              and sev["addOptions"]["monitor"] == "skip" and sev["addOptions"]["searchForMissingEpisodes"] is True)
        say("!request Sample Show s1", **dora)
        check("existing show, season already complete -> 'already complete'",
              expect_reply("season 1 is already complete") is not None)
        say("!request Sample Show s5", **dora)
        r = expect_reply("has no season")
        check("unknown season -> lists available seasons", r and "Available: 1, 2" in r["message"])

        erik = dict(sender=MEMBER5, name="Erik")
        say("!request Sample Show", **erik)
        r = expect_reply("which season")
        check("existing show: season list shows ✔️ vorhanden / ⏳ angefragt",
              r and "1. Season 1 (9 episodes) ✔️ available" in r["message"]
              and "2. Season 2 (10 episodes) ⏳ requested" in r["message"])
        say("!request 0", **erik)
        r = expect_reply("Requested:")
        put = puts[-1] if puts else {}
        check("'!request 0' on existing show -> all seasons monitored via PUT + SeriesSearch",
              r and "all seasons" in r["message"]
              and {s["seasonNumber"]: s["monitored"] for s in put.get("seasons", [])} == {0: False, 1: True, 2: True}
              and commands and commands[-1]["name"] == "SeriesSearch")
        check("series added only once (no duplicate POST)",
              sum(1 for _, b in added if b.get("tvdbId") == 9001) == 1)

        # ---------------- !releases / !grab / !release ----------------
        carla = dict(sender=MEMBER3, name="Carla")
        say("!releases Test Movie", **carla)
        expect_reply("Searching releases for 🎬 Test Movie")
        r = expect_reply("Releases for 🎬 Test Movie")
        check("member: !releases lists approved releases with quality/size/seeds",
              r and "1. Test.Movie.2016.2160p.UHD.BluRay.x265-TERMiNAL" in r["message"]
              and "30.0 GB · 50 seeds · TestIndexer" in r["message"])
        check("member: releases rejected by the profile are hidden", r and "720p" not in r["message"])
        say("!grab 9", **carla)
        check("!grab with invalid number -> asks for valid range", expect_reply("number from 1 to 2") is not None)
        say("!grab 1", **carla)
        r = expect_reply("Downloading")
        check("!grab 1 -> grabbed via POST /release (guid + indexerId)",
              r and any(b.get("guid") == "guid-Test.Movie.2016.2160p.UHD.BluRay.x265-TERMiNAL" and b.get("indexerId") == 3
                        for _, b in grabs))
        say("!release Test.Movie.2016.720p.HDTV.x264-LOL", **carla)
        r = expect_reply("Only an admin")
        check("member: !release of a rejected release -> refused with reason",
              r and "not wanted in profile" in r["message"] and not any("720p" in b["guid"] for _, b in grabs))
        say("!releases Sample Show Origins", **carla)
        r = expect_reply("Which season")
        check("series without season -> asks which season", r and "Available: 1" in r["message"])
        say("!releases s1", **carla)
        expect_reply("(without automatic search)")
        r = expect_reply("Releases for 📺 Sample Show: Origins (2024)** – Season 1")
        origin_show = next((b for k, b in added if k == "sonarr" and b.get("tvdbId") == 9002), None)
        check("!releases on a new show -> added WITHOUT auto search, then season list",
              r and origin_show and origin_show["addOptions"]["searchForMissingEpisodes"] is False
              and "Sample.Show.Origins.S01.1080p.WEB-DL-NTb" in r["message"])

        admin = dict(sender=ADMIN, name="Admin")
        say("!releases Test Movie", **admin)
        expect_reply("Searching releases for 🎬 Test Movie")
        r = expect_reply("Releases for 🎬 Test Movie")
        check("admin: sees rejected releases with reason", r and "720p.HDTV" in r["message"] and "⛔" in r["message"])
        say("!grab 3", **admin)
        r = expect_reply("Admin override")
        check("admin: may grab a rejected release", r and any("720p" in b["guid"] for _, b in grabs))
        say("!release Sample.Show.Origins.S01E02.1080p.WEB-DL-NTb", **admin)
        r = expect_reply("Downloading:** Sample.Show.Origins.S01E02")
        check("!release with a single-episode name -> episode search + grab",
              r and any(b["guid"] == "guid-Sample.Show.Origins.S01E02.1080p.WEB-DL-NTb" for _, b in grabs))
        say("!release Unknown.Movie.2020.1080p.WEB-DL-XYZ", **admin)
        r = expect_reply("can't find a movie/show")
        check("!release of an unknown title -> says nothing found (no '!request first' detour)",
              r and "!request" not in r["message"])
        say("!release Test.Movie.2016.2160p.FAKE-NOPE", **admin)
        check("!release not offered by indexers -> told so", expect_reply("The indexers don't have") is not None)
        say("!release Space.Movie.2014.1080p.BluRay.x264-SPARKS", **admin)
        r = expect_reply("Downloading:** Space.Movie")
        inter = next((b for k, b in added if b.get("tmdbId") == 8006), None)
        check("!release of a title not in the library -> added without auto search, then grabbed",
              r and inter and inter["addOptions"]["searchForMovie"] is False)

        # ---------------- !release with German release names ----------------
        erik = dict(sender=MEMBER5, name="Erik")
        say("!release Gruene.Wiese.1985.German.1080p.BluRay.x264-GRP", **erik)
        r = expect_reply("Downloading", timeout=20)
        bttf = next((b for _, b in added if b.get("tmdbId") == 8007), None)
        check("German release name (ue -> ü) -> found 'Green Meadow', added w/o search, grabbed",
              r and bttf and bttf["addOptions"]["searchForMovie"] is False
              and any(b["guid"] == "guid-Gruene.Wiese.1985.German.1080p.BluRay.x264-GRP" for _, b in grabs))

        fynn = dict(sender=MEMBER6, name="Fynn")
        say("!release Der.Grosse.Coup.S01.German.DL.1080p.WEB.x264-NTb", **fynn)
        r = expect_reply("Downloading", timeout=30)
        wrong_show = [b for _, b in added if b.get("tvdbId") == 9003]
        check("wrong first match (Coup Show) is rolled back, right show (The Big Coup) grabbed",
              r and wrong_show and any("/series/" in d for d in deletes)
              and not any(v.get("tvdbId") == 9003 for v in library.values())
              and any(v.get("tvdbId") == 9004 for v in library.values())
              and any(b["guid"] == "guid-Der.Grosse.Coup.S01.German.DL.1080p.WEB.x264-NTb" for _, b in grabs))
        check("rollback never deletes files", all("deleteFiles=false" in d for d in deletes))

        # ---------------- edge cases (0.8.0) ----------------
        g = dict(sender=MEMBER7, name="Gina")
        say("!request Forgotten", **g)
        r = expect_reply("Requested:")
        check("existing but UNmonitored movie -> monitored again + MoviesSearch (was: 'being searched')",
              r and "active again" in r["message"] and library[8]["monitored"] is True
              and any(c.get("name") == "MoviesSearch" and c.get("movieIds") == [8] for c in commands))
        say("!request Future Movie", **g)
        r = expect_reply("Requested:")
        check("not yet released movie -> tells expected date", r and "expected from 2099-05-01" in r["message"])
        say("!request", edit=True, **g)
        r = expect_reply("How to request")
        check("edited message (typo fixed) is processed, quoting the ORIGINAL message",
              r is not None and r.get("quote_timestamp") == r_ts_hint() - 5000)
        say("!request", sender=BOT, name="Bot-Owner", own=True, edit=True)
        r = expect_reply("How to request")
        check("OWN edited message (syncMessage.sentMessage.editMessage, as captured live) is processed",
              r is not None and r.get("quote_timestamp") == r_ts_hint() - 5000)
        say("\ufffc !request", **g)
        check("command after an @mention is recognised", expect_reply("How to request") is not None)

        h = dict(sender=MEMBER8, name="Hans")
        say("!request Sample Movie", **h)
        r = expect_reply("Results for")
        say("2", quote={"id": 1, "authorNumber": BOT, "text": r["message"] if r else ""}, **h)
        check("bare number as quote-reply to the bot's list works like '!request 2'",
              expect_reply("Sample Movie (1984)") is not None)
        say("2", **h)
        check("bare number WITHOUT quote is ignored", no_new_reply(1.5))
        say("!id", **h)
        r = expect_reply("privately")
        dm = expect_reply("Your Signal IDs")
        check("!id -> short note in group, IDs sent privately",
              r and dm and dm["recipients"] == [MEMBER8] and f"UUID: uuid-{MEMBER8}" in dm["message"])
        say("!request Sample Show 2022", **h)
        check("show with year in query (Sonarr finds it only without year) -> season question",
              expect_reply("which season") is not None)

        for _ in range(8):
            say("!request Spam", sender=OUTSIDER, group=None)
        check("DM spam from a stranger -> no reply at all (not even the spam-brake notice)",
              no_new_reply(2) and not any(m["recipients"] == [OUTSIDER] for m in sent))

        i = dict(sender=MEMBER9, name="Ida")
        say("!release `Test.Movie.2016.1080p.BluRay.x264-SPARKS.mkv`", **i)
        r = expect_reply("Downloading", timeout=20)
        check("pasted release name with backticks and .mkv -> cleaned and grabbed",
              r and "Test.Movie.2016.1080p.BluRay.x264-SPARKS" in r["message"])
        say("!release Test.Movie.2016.1080p.BluRey.x264-SPARKS", **i)
        r = expect_reply("doesn't exist like that", timeout=20)
        check("release name with a typo -> list of similar releases to !grab",
              r and "1. Test.Movie.2016.1080p.BluRay.x264-SPARKS" in r["message"] and "!grab" in r["message"])

        # ---------------- 0.9.1: "similar releases" must count towards the daily limit ----------------
        kai = dict(sender=MEMBER10, name="Kai")
        say("!release Typo.Movie.2015.1080p.BluRey.x264-SPARKS", **kai)
        r = expect_reply("doesn't exist like that", timeout=20)
        check("!release typo on a NEW title -> similar list says the title was added",
              r and "Typo Movie (2015) was added to Radarr so you can pick a release" in r["message"])
        say("!request Count Movie A", **kai)
        check("2nd title still within the limit (2)", expect_reply("Requested:") is not None)
        say("!request Count Movie B", **kai)
        check("3rd title refused -> the kept title from the similar list was counted (no limit bypass)",
              expect_reply("limit 2") is not None and not any(b.get("tmdbId") == 8014 for _, b in added))

        # Test Movie arrives -> announcement names the requester
        req = urllib.request.Request(
            f"http://127.0.0.1:{relay_port}/radarr", method="POST",
            data=json.dumps({"eventType": "Download", "movie": {"title": "Test Movie", "year": 2016,
                                                                 "tmdbId": 8001}}).encode(),
            headers={"Content-Type": "application/json", "X-Relay-Token": TOKEN})
        urllib.request.urlopen(req, timeout=10).read()
        r = expect_reply("New movie:** Test Movie")
        check("announcement says who requested it", r and "Requested by Anna" in r["message"])
    finally:
        chat_in.put(None)
        relay.terminate()
        relay.wait(15)
        log_file.close()
        server.shutdown()

    log_text = open(log_path, encoding="utf-8").read()
    check("API keys never written to the log", "radarrkey" not in log_text and "sonarrkey" not in log_text)
    check("phone numbers only masked in the log", MEMBER not in log_text)
    print(f"\n{len(sent)} replies, {len(added)} items added, {len(grabs)} releases grabbed, "
          f"{len(failures)} failed checks")
    if failures:
        print(f"relay log: {log_path}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
