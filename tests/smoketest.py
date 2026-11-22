#!/usr/bin/env python3
"""
End-to-end smoke test for relay.py – no Docker, no Signal account, no dependencies.

It starts relay.py as a real subprocess and points it at a fake server that plays
both the signal-cli-rest-api (POST /v2/send) and a poster/Jellyfin image host (GET).
Then it fires webhook payloads at the relay and checks what arrives at "Signal".

Run:  python tests/smoketest.py      (exit code 0 = all checks passed)
"""

import base64
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GROUP = "group." + base64.b64encode(b"smoketest-group").decode()
RELAY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "relay.py")
TOKEN = "s3cret-token-0123456789"
JELLYFIN_ID = "0123456789abcdef0123456789abcdef"

sent_to_signal: list[dict] = []          # bodies the relay POSTed to /v2/send
fetched: list[tuple[str, dict]] = []     # (path, headers) of every GET the relay made
flaky_attempts: list[float] = []         # send attempts for the "FLAKY" message
queue_calls: dict[str, int] = {}         # fake Sonarr/Radarr queue: calls per "seriesId=42" / "movieId=77"
BUSY = [{"status": "downloading", "trackedDownloadStatus": "ok", "trackedDownloadState": "downloading"}]
STUCK = [{"status": "completed", "trackedDownloadStatus": "warning", "trackedDownloadState": "importPending"}]


def fake_queue(query: str) -> list:
    """seriesId=42 busy for 3 calls, 43 always busy, 44 only a stuck item, movieId=77 busy for 2 calls."""
    queue_calls[query] = queue_calls.get(query, 0) + 1
    n = queue_calls[query]
    if query == "seriesId=42":
        return BUSY if n <= 3 else []
    if query == "seriesId=43":
        return BUSY
    if query == "seriesId=44":
        return STUCK
    if query == "movieId=77":
        return BUSY if n <= 2 else []
    return []
failures: list[str] = []


def check(name: str, ok) -> None:
    print(("PASS  " if ok else "FAIL  ") + name, flush=True)
    if not ok:
        failures.append(name)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeUpstream(BaseHTTPRequestHandler):
    """Fake Signal API + fake image host."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/api/v3/config/ui":                    # Radarr's "Movie Info Language" = German (4)
            body = json.dumps({"movieInfoLanguage": 4, "uiLanguage": 1}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/api/v3/movie/"):              # fake Radarr movie API (translated texts)
            if self.path != "/api/v3/movie/78":
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps({"id": 78, "title": "Übersetzter Film", "year": 2015,
                               "overview": "Eine deutsche Beschreibung."}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/api/v3/queue/details?"):     # fake Sonarr/Radarr queue
            body = json.dumps(fake_queue(self.path.split("?", 1)[1])).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        fetched.append((self.path, dict(self.headers)))
        if self.path.startswith("/redirect"):          # tries to bounce the relay to a foreign host
            self.send_response(302)
            self.send_header("Location", "http://evil.example/steal")
            self.end_headers()
            return
        if self.path.startswith("/json"):              # not an image
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")
            return
        self.send_response(200)                        # a "poster"
        self.send_header("Content-Type", "image/png")
        self.end_headers()
        self.wfile.write(b"\x89PNG-fake")

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        text = body.get("message", "")
        if "BREAK-READ" in text:
            # error response whose body breaks off – reading it raises inside the relay.
            # Before 0.9.1 this killed the only sender thread and all announcements stopped.
            self.send_response(500)
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(b"bro")
            self.close_connection = True
            return
        if "FLAKY" in text:
            flaky_attempts.append(time.time())
            if len(flaky_attempts) <= 4:           # Signal "down" for the first 4 attempts
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b'{"error": "temporarily unavailable"}')
                return
        sent_to_signal.append(body)
        self.send_response(201)
        self.end_headers()
        self.wfile.write(b"{}")


def main() -> int:
    upstream_port, relay_port = free_port(), free_port()
    upstream = f"http://127.0.0.1:{upstream_port}"
    server = ThreadingHTTPServer(("127.0.0.1", upstream_port), FakeUpstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    env = dict(os.environ, SIGNAL_API_URL=upstream, SIGNAL_NUMBER="+4900000000", SIGNAL_RECIPIENT=GROUP,
               LISTEN_HOST="127.0.0.1", LISTEN_PORT=str(relay_port), AGGREGATE_SECONDS="1",
               ANNOUNCE_TESTS="true", SEND_POSTERS="true", POSTER_ALLOWED_HOSTS="127.0.0.1",
               JELLYFIN_URL=upstream, JELLYFIN_API_KEY="jfkey", PYTHONIOENCODING="utf-8",
               SIGNAL_RETRY_INTERVAL="1", SIGNAL_RETRY_WINDOW="8",
               SONARR_URL=upstream, SONARR_API_KEY="sonarrkey", RADARR_URL=upstream, RADARR_API_KEY="radarrkey",
               QUEUE_RECHECK_SECONDS="1", ANNOUNCE_MAX_WAIT_SECONDS="8")

    # 1) Startup refuses insecure configuration
    for token, label in (("", "missing"), ("change-me", "placeholder"), ("short", "too short")):
        p = subprocess.run([sys.executable, RELAY], env=dict(env, RELAY_TOKEN=token),
                           capture_output=True, text=True, timeout=15)
        check(f"refuses to start with {label} token", p.returncode != 0 and "Configuration error" in p.stderr)
    p = subprocess.run([sys.executable, RELAY], env=dict(env, RELAY_TOKEN=TOKEN, BOT_LANGUAGE="fr"),
                       capture_output=True, text=True, timeout=15)
    check("refuses to start with an unknown BOT_LANGUAGE", p.returncode != 0 and "BOT_LANGUAGE" in p.stderr)

    # 2) Start the relay for real (DEBUG log goes to a temp file)
    log_path = os.path.join(tempfile.gettempdir(), "signal-media-relay-test.log")
    log_file = open(log_path, "w", encoding="utf-8")
    relay = subprocess.Popen([sys.executable, RELAY], env=dict(env, RELAY_TOKEN=TOKEN, LOG_LEVEL="DEBUG"),
                             stdout=log_file, stderr=log_file)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{relay_port}/health", timeout=1)
                break
            except OSError:
                time.sleep(0.1)

        def post(path, data, headers=None, query=""):
            body = data if isinstance(data, bytes) else json.dumps(data).encode()
            req = urllib.request.Request(f"http://127.0.0.1:{relay_port}{path}{query}",
                                         data=body,
                                         headers={"Content-Type": "application/json", **(headers or {})})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code

        def movie(title, year, poster_url):
            return {"eventType": "Download", "movie": {"title": title, "year": year,
                    "images": [{"coverType": "poster", "remoteUrl": poster_url}]}}

        # 3) Authentication and input validation
        auth = {"X-Relay-Token": TOKEN}
        basic = {"Authorization": "Basic " + base64.b64encode(f"relay:{TOKEN}".encode()).decode()}
        check("no token -> 401", post("/sonarr", {"eventType": "Test"}) == 401)
        check("wrong token -> 401", post("/sonarr", {"eventType": "Test"}, {"X-Relay-Token": "nope"}) == 401)
        check("Basic Auth (Sonarr/Radarr password) -> 200", post("/sonarr", {"eventType": "Test"}, basic) == 200)
        check("X-Relay-Token header (Jellyfin) -> 200", post("/radarr", {"eventType": "Test"}, auth) == 200)
        check("?token= query -> 200", post("/jellyfin", {"NotificationType": "Test"}, query=f"?token={TOKEN}") == 200)
        check("non-object JSON -> 400", post("/sonarr", "x", auth) == 400)
        check("wrong field types -> handled, no 500",
              post("/sonarr", {"eventType": "Download", "series": "x", "episodes": "y"}, auth) == 200)

        # 4) Malicious / tricky payloads (each one still produces an announcement)
        post("/radarr", movie("FileUrl", 2001, "file:///etc/passwd"), auth)
        post("/radarr", movie("Metadata", 2002, "http://169.254.169.254/latest/meta-data"), auth)
        post("/radarr", movie("Redirect", 2003, f"{upstream}/redirect"), auth)
        post("/radarr", movie("NotImage", 2004, f"{upstream}/json"), auth)
        post("/radarr", movie("Good\nInjected‮Line", 2005, f"{upstream}/poster.png"), auth)
        post("/jellyfin", {"NotificationType": "ItemAdded", "ItemType": "Movie", "Name": "Traversal",
                           "Year": "2006", "ItemId": "../../System/Info?x="}, auth)
        post("/jellyfin", {"NotificationType": "ItemAdded", "ItemType": "Episode", "Name": "Pilot",
                           "SeriesName": "Cat &amp; Mouse", "SeasonNumber": "1", "EpisodeNumber": "1",
                           "SeriesId": JELLYFIN_ID}, auth)
        # 5) Episode bundling + de-duplication: 3 episodes -> 1 message, repeat is dropped
        for ep in (1, 2, 3, 2):
            post("/sonarr", {"eventType": "Download", "series": {"title": "Bundle Show"},
                             "episodes": [{"seasonNumber": 1, "episodeNumber": ep, "title": f"Ep {ep}"}]}, auth)
        post("/radarr", movie("FileUrl", 2001, ""), auth)   # same movie again -> dropped
        # 6) edge cases (0.8.0)
        # Jellyfin "Generic Form": form data, but the header claims application/json
        form_code = post("/jellyfin", b"NotificationType=ItemAdded&ItemType=Movie&ItemId=&Name=Form%20Movie"
                                      b"&Year=2021&SeriesName=&SeriesId=&SeasonNumber=&EpisodeNumber=", auth)
        form_ep = post("/jellyfin", b"NotificationType=ItemAdded&ItemType=Episode&ItemId=&Name=Pilot"
                                    b"&Year=&SeriesName=Form%20%26amp%3B%20Show&SeriesId=&SeasonNumber=2"
                                    b"&EpisodeNumber=5", auth)
        post("/radarr", movie("St*ar ~Special~ `x` ||y||", 2010, ""), auth)   # Signal formatting markers
        post("/radarr", movie("東京物語", 2020, ""), auth)                          # non-Latin titles
        post("/radarr", movie("七人の侍", 2020, ""), auth)                          # same year, different title
        many = [post("/sonarr", {"eventType": "Download", "series": {"title": f"Show {n}"},
                                 "episodes": [{"seasonNumber": 1, "episodeNumber": 1, "title": "Pilot"}]}, auth)
                for n in range(70)]                                              # large import
        # 0.9.1: a broken Signal error response must not kill the sender; a flaky Signal is retried
        post("/radarr", movie("BREAK-READ Movie", 2011, ""), auth)
        post("/radarr", movie("After Break", 2012, ""), auth)
        post("/radarr", movie("FLAKY Movie", 2013, ""), auth)
        # 0.9.2: wait for an empty queue per show/movie; one message per show across seasons
        def show(title, sid, season, ep):
            return {"eventType": "Download", "series": {"title": title, "id": sid},
                    "episodes": [{"seasonNumber": season, "episodeNumber": ep, "title": f"Ep {season}-{ep}"}]}
        post("/sonarr", show("Queue Show", 42, 1, 1), auth)
        post("/sonarr", show("Stuck Show", 43, 1, 1), auth)
        post("/sonarr", show("Failed Show", 44, 1, 1), auth)
        post("/radarr", {"eventType": "Download", "movie": {"title": "Queued Movie", "year": 2014, "id": 77}}, auth)
        # 0.10.2: Radarr is set to German, the bot to English -> the (English) webhook text is used
        post("/radarr", {"eventType": "Download", "movie": {"title": "Translated Movie", "year": 2015, "id": 78,
                                                            "overview": "An English description."}}, auth)
        time.sleep(1.5)
        post("/sonarr", show("Queue Show", 42, 2, 3), auth)     # arrives while the show is still downloading

        expected = 3 + 7 + 1 + 2 + 3 + 70 + 2 + 4 + 1   # ... + After Break, FLAKY + 4 queue scenarios + translated
        deadline = time.time() + 60
        while len(sent_to_signal) < expected and time.time() < deadline:
            time.sleep(0.2)
        time.sleep(1.5)   # give a wrongly-sent duplicate the chance to show up
        try:
            health_after = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{relay_port}/health",
                                                             timeout=5).read())
        except urllib.error.HTTPError as exc:   # 503 = unhealthy, still has a JSON body
            health_after = json.loads(exc.read())
    finally:
        relay.terminate()
        relay.wait(15)
        log_file.close()
    log_text = open(log_path, encoding="utf-8").read()

    # 6) Evaluate what arrived at "Signal"
    def message(word):
        return next((m for m in sent_to_signal if word in m["message"].splitlines()[0]), None)

    def no_attachment(word):
        m = message(word)
        return m is not None and "base64_attachments" not in m

    paths = [p for p, _ in fetched]
    check("file:// poster refused, movie still announced", no_attachment("FileUrl"))
    check("non-allowlisted host never contacted", no_attachment("Metadata"))
    check("redirect to foreign host blocked", "/redirect" in paths and no_attachment("Redirect"))
    check("non-image response not attached", no_attachment("NotImage"))
    good = message("Good")
    check("allowed poster attached", bool(good and good.get("base64_attachments")))
    check("newline + bidi chars stripped from title",
          bool(good and "Good Injected Line" in good["message"] and "‮" not in good["message"]))
    check("Jellyfin path-traversal ID not used", not any("System" in p for p in paths))
    tom = message("Cat")
    check("Jellyfin HTML entities decoded", bool(tom and "Cat & Mouse" in tom["message"]))
    jf = [h for p, h in fetched if p.startswith(f"/Items/{JELLYFIN_ID}")]
    check("Jellyfin poster fetched with API key", bool(jf and jf[0].get("X-Emby-Token") == "jfkey"))
    bundle = [m for m in sent_to_signal if "Bundle Show" in m["message"]]
    check("3 episodes bundled into 1 message", len(bundle) == 1 and "3 new episodes" in bundle[0]["message"])
    check("duplicate movie not announced twice", sum("FileUrl" in m["message"] for m in sent_to_signal) == 1)
    check("all messages go to the configured group", all(m["recipients"] == [GROUP] for m in sent_to_signal))
    check("token never written to the log", TOKEN not in log_text)
    star = message("St")
    check("formatting markers in titles are neutralised (no broken bold/strike/spoiler)",
          bool(star and "St∗ar ∼Special∼ ˋxˋ ∣∣y∣∣" in star["message"]))
    check("two different non-Latin titles of the same year are both announced (no false duplicate)",
          bool(message("東京物語") and message("七人の侍")))
    form_movie, form_show = message("Form Movie"), message("Form & Show")
    check("Jellyfin 'Generic Form' (form data, wrong JSON header) -> movie and episode announced",
          form_code == 200 and form_ep == 200 and bool(form_movie and "(2021)" in form_movie["message"])
          and bool(form_show and "S02E05 – Pilot" in form_show["message"]))
    check("70 different shows at once -> all accepted and announced (single flusher thread)",
          all(code == 200 for code in many)
          and sum(1 for m in sent_to_signal if re.search(r":\*\* Show \d+ – ", m["message"])) == 70)
    check("default bot language is English", bool(message("New movie") and not message("Neuer Film")))
    check("warning in the log when BOT_LANGUAGE is not set (0.9.0 changed the default)",
          "BOT_LANGUAGE is not set" in log_text)
    check("broken Signal error response does not kill the sender (later messages still arrive)",
          bool(message("After Break")) and not message("BREAK-READ"))
    check("Signal down for 4 attempts -> message kept in the retry list and delivered later",
          bool(message("FLAKY Movie")) and len(flaky_attempts) >= 5)
    queue_show = [m["message"] for m in sent_to_signal if "Queue Show" in m["message"]]
    check("show still downloading -> waits, then ONE message across seasons (not one per season/batch)",
          len(queue_show) == 1 and "2 new episodes:** Queue Show – Seasons 1, 2" in queue_show[0]
          and "S01E01" in queue_show[0] and "S02E03" in queue_show[0] and queue_calls.get("seriesId=42", 0) >= 4)
    check("download stuck forever -> announced anyway after ANNOUNCE_MAX_WAIT_SECONDS",
          bool(message("Stuck Show")) and "announcing anyway" in log_text)
    check("only a stuck/failed queue item (warning) -> does not block the announcement",
          bool(message("Failed Show")) and queue_calls.get("seriesId=44", 0) >= 1)
    queued_movie = [m["message"] for m in sent_to_signal if "Queued Movie" in m["message"]]
    check("movie waits for Radarr's queue, then is announced on its own",
          len(queued_movie) == 1 and queued_movie[0].startswith("🎬") and queue_calls.get("movieId=77", 0) >= 3)
    english = message("Translated Movie")
    check("BOT_LANGUAGE=en, Radarr set to German -> English webhook title/plot + hint in the log",
          bool(english and "*An English description.*" in english["message"]) and not message("Übersetzter Film")
          and "Movie Info Language = English" in log_text)
    check("/health reports running sender/flusher", health_after.get("status") == "ok"
          and health_after.get("sender") == "running" and health_after.get("flusher") == "running")

    # 7b) Signal unreachable -> /health turns 503 after the grace period
    dead_port, hp = free_port(), free_port()
    unhealthy = subprocess.Popen([sys.executable, RELAY], env=dict(
        env, RELAY_TOKEN=TOKEN, LISTEN_PORT=str(hp), SIGNAL_API_URL=f"http://127.0.0.1:{dead_port}",
        HEALTH_GRACE_SECONDS="1", BOT_LANGUAGE="en"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    health_code, health_body = 0, {}
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{hp}/health", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        urllib.request.urlopen(urllib.request.Request(
            f"http://127.0.0.1:{hp}/radarr", data=json.dumps(movie("Nobody Hears", 2020, "")).encode(),
            method="POST", headers={"Content-Type": "application/json", "X-Relay-Token": TOKEN}), timeout=10).read()
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{hp}/health", timeout=2)
            except urllib.error.HTTPError as exc:
                health_code, health_body = exc.code, json.loads(exc.read())
                break
            time.sleep(0.5)
    finally:
        unhealthy.terminate()
        unhealthy.wait(15)
    check("Signal unreachable -> /health returns 503 with the reason",
          health_code == 503 and any("Signal API unreachable" in p for p in health_body.get("problems", [])))

    # 7) BOT_LANGUAGE=de: same relay, German texts
    de_port = free_port()
    before = len(sent_to_signal)
    de_relay = subprocess.Popen([sys.executable, RELAY], env=dict(env, RELAY_TOKEN=TOKEN, BOT_LANGUAGE="de",
                                                                 LISTEN_PORT=str(de_port)),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{de_port}/health", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        for path, payload in (("/radarr", {"eventType": "Download", "movie": {"title": "Deutsch Test", "year": 2001},
                                           "movieFile": {"quality": "WEBDL-1080p"}}),
                              ("/radarr", {"eventType": "Download",
                                           "movie": {"title": "Translated Movie", "year": 2015, "id": 78,
                                                     "overview": "An English description."}}),
                              ("/radarr", {"eventType": "Download",          # no translation (404) -> webhook
                                           "movie": {"title": "Webhook Only", "year": 2016, "id": 79,
                                                     "overview": "Fallback text."}}),
                              ("/sonarr", {"eventType": "Download", "series": {"title": "Deutsch Serie"},
                                           "episodes": [{"seasonNumber": 3, "episodeNumber": 1}]})):
            urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:{de_port}{path}", data=json.dumps(payload).encode(), method="POST",
                headers={"Content-Type": "application/json", "X-Relay-Token": TOKEN}), timeout=10).read()
        deadline = time.time() + 15
        while len(sent_to_signal) < before + 4 and time.time() < deadline:
            time.sleep(0.2)
    finally:
        de_relay.terminate()
        de_relay.wait(15)
    german = [m["message"] for m in sent_to_signal[before:]]
    check("BOT_LANGUAGE=de -> German announcements",
          any("🎬 **Neuer Film:** Deutsch Test (2001)" in m and "Qualität: WEBDL-1080p" in m for m in german)
          and any("**Neue Folge:** Deutsch Serie – Staffel 3" in m for m in german))
    check("BOT_LANGUAGE=de + Radarr German -> German title and plot from Radarr's API",
          any("**Neuer Film:** Übersetzter Film (2015)" in m and "*Eine deutsche Beschreibung.*" in m
              for m in german))
    check("no translation available (404) -> webhook text is used",
          any("Webhook Only" in m and "*Fallback text.*" in m for m in german))

    # DISPLAY_MOVIE_PLOT=false -> no plot under the movie (format checked in a separate interpreter)
    code = ("import relay; m = relay.PendingMovie(title='T', year='2001', overview='Plot text', quality='', "
            "poster=None, source='Radarr'); print(relay.Relay._format_movie(m))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=15,
                         cwd=os.path.dirname(RELAY),
                         env=dict(env, RELAY_TOKEN=TOKEN, DISPLAY_MOVIE_PLOT="false", PYTHONIOENCODING="utf-8"))
    check("DISPLAY_MOVIE_PLOT=false -> no plot under the movie",
          out.returncode == 0 and "T (2001)" in out.stdout and "Plot text" not in out.stdout)

    print(f"\n{len(sent_to_signal)} messages sent to fake Signal, {len(failures)} failed checks")
    server.shutdown()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
