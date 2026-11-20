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
        sent_to_signal.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
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
               JELLYFIN_URL=upstream, JELLYFIN_API_KEY="jfkey", PYTHONIOENCODING="utf-8")

    # 1) Startup refuses insecure configuration
    for token, label in (("", "missing"), ("change-me", "placeholder"), ("short", "too short")):
        p = subprocess.run([sys.executable, RELAY], env=dict(env, RELAY_TOKEN=token),
                           capture_output=True, text=True, timeout=15)
        check(f"refuses to start with {label} token", p.returncode != 0 and "Konfigurationsfehler" in p.stderr)

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
                           "SeriesName": "Katz &amp; Maus", "SeasonNumber": "1", "EpisodeNumber": "1",
                           "SeriesId": JELLYFIN_ID}, auth)
        # 5) Episode bundling + de-duplication: 3 episodes -> 1 message, repeat is dropped
        for ep in (1, 2, 3, 2):
            post("/sonarr", {"eventType": "Download", "series": {"title": "Bundle Show"},
                             "episodes": [{"seasonNumber": 1, "episodeNumber": ep, "title": f"Ep {ep}"}]}, auth)
        post("/radarr", movie("FileUrl", 2001, ""), auth)   # same movie again -> dropped
        # 6) Edge cases (0.8.0)
        # Jellyfin "Generic Form": Formulardaten, aber Header behauptet application/json
        form_code = post("/jellyfin", b"NotificationType=ItemAdded&ItemType=Movie&ItemId=&Name=Formular%20Film"
                                      b"&Year=2021&SeriesName=&SeriesId=&SeasonNumber=&EpisodeNumber=", auth)
        form_ep = post("/jellyfin", b"NotificationType=ItemAdded&ItemType=Episode&ItemId=&Name=Pilot"
                                    b"&Year=&SeriesName=Formular%20%26amp%3B%20Serie&SeriesId=&SeasonNumber=2"
                                    b"&EpisodeNumber=5", auth)
        post("/radarr", movie("Stern*chen ~Spezial~ `x` ||y||", 2010, ""), auth)   # Signal-Formatierung
        post("/radarr", movie("東京物語", 2020, ""), auth)                          # nicht-lateinische Titel
        post("/radarr", movie("七人の侍", 2020, ""), auth)                          # gleiches Jahr, anderer Titel
        many = [post("/sonarr", {"eventType": "Download", "series": {"title": f"Serie {n}"},
                                 "episodes": [{"seasonNumber": 1, "episodeNumber": 1, "title": "Pilot"}]}, auth)
                for n in range(70)]                                              # grosser Import

        expected = 3 + 7 + 1 + 2 + 3 + 70   # 3 test pings, 7 announcements above, 1 bundled episode message
        deadline = time.time() + 30
        while len(sent_to_signal) < expected and time.time() < deadline:
            time.sleep(0.2)
        time.sleep(1.5)   # give a wrongly-sent duplicate the chance to show up
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
    tom = message("Katz")
    check("Jellyfin HTML entities decoded", bool(tom and "Katz & Maus" in tom["message"]))
    jf = [h for p, h in fetched if p.startswith(f"/Items/{JELLYFIN_ID}")]
    check("Jellyfin poster fetched with API key", bool(jf and jf[0].get("X-Emby-Token") == "jfkey"))
    bundle = [m for m in sent_to_signal if "Bundle Show" in m["message"]]
    check("3 episodes bundled into 1 message", len(bundle) == 1 and "3 neue Folgen" in bundle[0]["message"])
    check("duplicate movie not announced twice", sum("FileUrl" in m["message"] for m in sent_to_signal) == 1)
    check("all messages go to the configured group", all(m["recipients"] == [GROUP] for m in sent_to_signal))
    check("token never written to the log", TOKEN not in log_text)
    star = message("Stern")
    check("formatting markers in titles are neutralised (no broken bold/strike/spoiler)",
          bool(star and "Stern∗chen ∼Spezial∼ ˋxˋ ∣∣y∣∣" in star["message"]))
    check("two different non-Latin titles of the same year are both announced (no false duplicate)",
          bool(message("東京物語") and message("七人の侍")))
    form_movie, form_show = message("Formular Film"), message("Formular & Serie")
    check("Jellyfin 'Generic Form' (form data, wrong JSON header) -> movie and episode announced",
          form_code == 200 and form_ep == 200 and bool(form_movie and "(2021)" in form_movie["message"])
          and bool(form_show and "S02E05 – Pilot" in form_show["message"]))
    check("70 different shows at once -> all accepted and announced (single flusher thread)",
          all(code == 200 for code in many)
          and sum(1 for m in sent_to_signal if re.search(r":\*\* Serie \d+ – ", m["message"])) == 70)

    print(f"\n{len(sent_to_signal)} messages sent to fake Signal, {len(failures)} failed checks")
    server.shutdown()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
