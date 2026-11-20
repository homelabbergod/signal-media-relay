# Signal Media Relay

**Version 0.9.0** · see [CHANGELOG](CHANGELOG.md)

Posts new movies and episodes from **Sonarr/Radarr** (or **Jellyfin**) into a **Signal group**.

```
Sonarr / Radarr / Jellyfin ──webhook──▶ relay :8090 ──▶ signal-cli-rest-api :8080 ──▶ Signal group
```

```
🎬 New movie: Example Movie (2024)            📺 3 new episodes: Example Show – Season 2
Quality: Bluray-1080p                         S02E01 – Episode One
A short description …             [poster]   S02E02 – Episode Two …            [poster]
```

- Single Python file, standard library only. No `pip install` needed.
- Bundles a season's episodes into one message and never posts the same item twice within 24 h.
- Skips quality upgrades by default and attaches posters.
- Hardened: token auth, SSRF-safe poster downloads, non-root read-only container.
- Bot messages in English or German (`BOT_LANGUAGE=en|de`).

## Install

**You need:** Docker with Compose, and a phone number for the bot. A second number is recommended. You can also link your own account as a device.

### 1. Get the code and configure

```bash
git clone <this-repo> signal-media-relay && cd signal-media-relay
git update-index --skip-worktree docker-compose.yml          # your real values never get committed
python -c "import secrets;print(secrets.token_urlsafe(24))"   # → RELAY_TOKEN
```

All settings live in the `environment:` block of the `relay` service in **`docker-compose.yml`**. Every option there is commented. The repo version contains only placeholders.

### 2. Start the Signal API and link the bot account

```bash
docker compose up -d signal-api
```

Open `http://localhost:8080/v1/qrcodelink?device_name=media-relay` in a browser. In the Signal app, scan it via **Settings → Linked devices → +**.

### 3. Create the group and get its ID

In the Signal app, create a group with the bot account in it. Then run:

```bash
curl http://localhost:8080/v1/groups/+491701234567     # your bot number
```

Copy the `"id"` value (starts with `group.`) into `docker-compose.yml`:

```yaml
    environment:
      SIGNAL_NUMBER: "+491701234567"
      SIGNAL_RECIPIENT: "group.xxxxxxxx"
      RELAY_TOKEN: "<token from step 1>"
      BOT_LANGUAGE: "en"                # or "de"
```

> Signal has no broadcast channels. To make the group behave like one, set **Group settings → Send messages → Only admins**.

### 4. Start the relay

```bash
docker compose up -d --build
docker compose logs -f relay        # should say "Signal Media Relay 0.9.0 listening on 0.0.0.0:8090"
```

### 5. Connect a source

**Sonarr and Radarr (recommended):** go to **Settings → Connect → + → Webhook**.

| Field | Value |
|---|---|
| Triggers | ✅ On File Import (optionally On File Upgrade) |
| URL | `http://<relay-host>:8090/sonarr`, or `/radarr` for Radarr |
| Method | `POST` |
| Username / Password | `relay` / `<RELAY_TOKEN>` |

Click **Test**. With `ANNOUNCE_TESTS=true`, a ✅ message appears in the group.

**Jellyfin (alternative):** install the **Webhook** plugin, then add a **Generic Destination**. *Generic Form* works too: it sends form data instead of JSON, and the relay accepts both.

| Field | Value |
|---|---|
| Webhook URL | `http://<relay-host>:8090/jellyfin` |
| Notification type | ✅ Item Added |
| Item type | ✅ Movies, ✅ Episodes |
| Template | contents of [`jellyfin-template.json`](jellyfin-template.json) |
| Headers | `Content-Type: application/json` and `X-Relay-Token: <RELAY_TOKEN>` |

For posters and descriptions from Jellyfin, also set `JELLYFIN_URL` and `JELLYFIN_API_KEY` in `docker-compose.yml`.

Use **one** source per library. If you use both, duplicate protection catches most double posts.

## Requests from the chat (`!request`)

Group members can request a movie or a show directly from Signal:

```
Anna:  !request Example Movie
Bot:   🔍 Results for “Example Movie”:
       1. 🎬 Example Movie (2021)
       2. 🎬 Example Movie (1984)
       3. 📺 Example Show: Origins (2024) – 1 season
       Reply with “!request <no.>” (valid for 10 min)
Anna:  !request 1
Bot:   ✅ Requested: 🎬 Example Movie (2021)
       Radarr is searching for it now – it will be announced here once it's there.
 …later
Bot:   🎬 New movie: Example Movie (2021) · 🙋 Requested by Anna
```

| Command | What it does |
|---|---|
| `!request <title>` | Searches Radarr and Sonarr. A single exact match is used right away; otherwise you get a numbered list. **Movies** are added and downloaded according to the quality profile. For **shows**, the bot asks which season (`0` = all) and Sonarr downloads according to the quality profile. |
| `!request <title> s<season>` | Show with the season given directly, e.g. `!request Example Show s2`. Skips the question. |
| `!request <title> <year>` | Narrows down the search, e.g. `!request Example Movie 2021` |
| `!request movie <title>` / `!request show <title>` | Searches movies only / shows only (German: `film`/`serie`) |
| `!request <number>` | Picks an entry from your last list |
| `!releases <title>` | Lists the indexer releases, like *Interactive Search* in Radarr/Sonarr. For shows add the season: `!releases Example Show s2` |
| `!releases` | The same list for the title you picked last |
| `!grab <number>` | Downloads that release from your last `!releases` list |
| `!release <release name>` | Downloads exactly this release, e.g. `!release Example.Movie.2024.2160p.WEB-DL.DDP5.1-GROUP`. No `!request` needed first: the bot works out the title from the name, adds it if needed and grabs it. German release names work too (`Gruene.Wiese…` is found as *Grüne Wiese* or under its original title). Single episodes (`…S02E05…`) and season packs (`…S02…`) work. |
| `!id` | Sends you your Signal number/UUID privately, for `REQUEST_ADMINS` / `REQUEST_ALLOWED_SENDERS`. People who hide their number in Signal can only be listed by UUID. |
| `!request` / `!help` / `!hilfe` | Shows the help |

Also handy:
- **Lists:** you can reply to a bot list with just the number, as a quote (`2` instead of `!request 2`).
- **Edits:** edited messages are processed too, e.g. a typo fixed after sending.
- **Mentions:** commands after an @mention of the bot work.

**Picking a release:**
- Members only see and download releases that Radarr/Sonarr **approve** for your quality profile.
- Numbers in `REQUEST_ADMINS` also see rejected releases, with the reason, and can download them. Admins have no daily limit.
- If the title isn't in the library yet, `!releases`/`!release` first add it **without** automatic search, so only the chosen release is downloaded.
- How `!release` finds the title: it searches with the name from the release, also with umlauts restored (`ue` → `ü`). It then checks up to 3 matches to see whether the indexers offer **exactly this release** for that title. A match the bot added for this check and that turns out to be wrong is removed again (no files, no exclusion list).
- The daily limit counts **titles**: `!request Example Movie` followed by `!grab` for Example Movie uses one request.

For shows it looks like this:

```
Ben:  !request Example Show
Bot:  📺 Example Show (2022) – which season?
      0. All seasons
      1. Season 1 (9 episodes) ✔️ available
      2. Season 2 (10 episodes)
Ben:  !request 2
Bot:  ✅ Requested: 📺 Example Show (2022) – season 2
      Sonarr is now searching according to the quality profile …
```

- **New show:** only the chosen seasons are monitored and searched.
- **Show already in Sonarr:** the chosen seasons are monitored as well and searched. Seasons that are already complete are reported as such.
- **Only one season:** the question is skipped.

The bot replies to each request with a quote. It reports success and every kind of failure:

- nothing found
- already available, already requested, or already in Radarr/Sonarr
- Radarr/Sonarr unreachable, wrong API key, or rejected by Radarr/Sonarr
- daily limit reached
- list expired
- too many commands at once

**Setup:**
1. In `docker-compose.yml`, set `REQUESTS_ENABLED: "true"`, plus `RADARR_URL`/`RADARR_API_KEY` and/or `SONARR_URL`/`SONARR_API_KEY`. The API key is under *Settings → General → Security*. Optionally pin the quality profile and root folder.
2. The relay container must be able to reach Radarr/Sonarr: same Docker network (`http://radarr:7878`) or a LAN IP.
3. Run `docker compose up -d`. The log should show `Radarr reachable (version …)` and `Signal receiver connected`.

**Who can request:**
- Anyone who writes in the group.
- If the group is set to "only admins can send", members can DM the bot instead (`REQUEST_VIA_DM=true`). Only DMs from members of the group are answered.
- To restrict requests to specific people, use `REQUEST_ALLOWED_SENDERS`.

**Built-in protection:**
- Each person gets `REQUEST_LIMIT_PER_DAY` successful requests per 24 h.
- At most 6 commands per person per minute.
- Commands older than 1 h are ignored.
- The bot only ever **adds** titles and starts downloads. It never deletes anything or changes settings. The one exception: if `!release` added a title just to check it and that title turns out to be wrong, it removes it again (no files, no exclusion list).
- DMs from people who are not in the group are dropped without a reply.
- Admin overrides are only possible for numbers listed in `REQUEST_ADMINS`.

> If the bot uses **your own** Signal account (linked device), your own `!request` messages work too.

## Unraid

1. **Image:** `homelabbergod/signal-media-relay` on Docker Hub. To build and push a new version:
   `docker build -t homelabbergod/signal-media-relay:0.9.0 -t homelabbergod/signal-media-relay:latest . && docker push --all-tags homelabbergod/signal-media-relay`
2. **Template:** copy [`unraid/my-signal-media-relay.xml`](unraid/my-signal-media-relay.xml) to `/boot/config/plugins/dockerMan/templates-user/`. Then go to *Docker → Add Container → Template* and pick `signal-media-relay`. All variables are listed there with descriptions, secrets are masked, and the hardening options are preset as *Extra Parameters*.
3. **Signal API:** `bbernhard/signal-cli-rest-api` with `MODE=json-rpc`, available from Community Apps or via *Add Container*. Map `/home/.local/share/signal-cli` to `/mnt/user/appdata/signal-cli-rest-api`.
4. **Network (with gluetun):** put the relay and the Signal API into the **custom Docker network that gluetun is in**. Radarr/Sonarr stay inside gluetun's network stack (`--net=container:gluetun`).

   | Connection | Address |
   |---|---|
   | Relay → Radarr/Sonarr | `http://gluetun:7878`, `http://gluetun:8989` |
   | Radarr/Sonarr → Relay (webhook) | `http://signal-media-relay:8090/radarr`, `/sonarr` |
   | Relay → Signal API | `http://signal-api:8080` |

   Radarr and Sonarr never get their own network interface. Their only way out is gluetun's VPN tunnel.

## Configuration (`environment:` in `docker-compose.yml`)

Changes take effect with `docker compose up -d`. A plain `restart` keeps the old values.

| Variable | Default | Purpose |
|---|---|---|
| `SIGNAL_NUMBER` | – | Bot's number (required) |
| `SIGNAL_RECIPIENT` | – | `group.…` ID or a phone number (required) |
| `RELAY_TOKEN` | – | Webhook secret, ≥ 16 chars (required) |
| `BOT_LANGUAGE` | `en` | Language of the bot messages: `en` or `de` |
| `AGGREGATE_SECONDS` | `60` | How long to collect episodes of a season before posting |
| `DEDUPE_HOURS` | `24` | Don't repost the same item within this window (`0` = off) |
| `ANNOUNCE_UPGRADES` | `false` | Also post quality upgrades |
| `ANNOUNCE_TESTS` | `false` | Post "Test" button events into the group |
| `SEND_POSTERS` | `true` | Attach poster image |
| `POSTER_ALLOWED_HOSTS` | tvdb, tmdb, fanart | Only download posters from these hosts |
| `JELLYFIN_URL` / `JELLYFIN_API_KEY` | – | Posters and descriptions for the Jellyfin source |
| `REQUESTS_ENABLED` | `false` | Turn on `!request` |
| `RADARR_URL` / `RADARR_API_KEY` | – | Radarr for movie requests |
| `SONARR_URL` / `SONARR_API_KEY` | – | Sonarr for show requests |
| `*_QUALITY_PROFILE` / `*_ROOT_FOLDER` | first one | Where requested titles go |
| `RADARR_MINIMUM_AVAILABILITY` | `released` | When Radarr starts searching |
| `SONARR_MONITOR` | `all` | Which episodes Sonarr monitors |
| `REQUEST_LIMIT_PER_DAY` | `5` | Successful requests per person per 24 h (`0` = unlimited) |
| `REQUEST_VIA_DM` | `true` | Accept DMs from group members |
| `REQUEST_ALLOWED_SENDERS` | – | Only these numbers/UUIDs may request |
| `REQUEST_ADMINS` | – | May pick releases the profile rejects; no daily limit |
| `RELEASES_SHOWN` | `8` | Releases listed by `!releases` |
| `RELEASE_SEARCH_TIMEOUT` | `180` | Seconds to wait for the indexers |
| `REQUEST_COMMANDS_PER_MINUTE` | `6` | Spam brake per person |
| `LOG_LEVEL` | `INFO` | `DEBUG` logs every incoming payload (token redacted) |

## Test

```bash
python tests/smoketest.py       # announcements: auth, bundling, de-duplication, security protections
python tests/test_requests.py   # !request: search, selection, adding, error replies, limits, DMs
```

Both tests run the relay against fake Signal and Radarr/Sonarr servers. They need no Docker, no Signal account and no media server.

## Security notes

- **Never expose port 8080 (signal-cli-rest-api):** it has no authentication. `signal-cli-config/` holds the account's private keys, so keep it safe and back it up.
- **The relay speaks plain HTTP.** Keep it on your LAN or Docker network, or put a TLS reverse proxy in front of it.
- **The pinned `signal-cli-rest-api:0.101` has known CVEs** that only its maintainer can fix. Docker Scout (20 Nov 2026) reports 7 critical and 42 high-severity fixable ones in Go, curl, the JDK and Jackson. Bump the tag when a new release appears, and check it with `docker scout quickview bbernhard/signal-cli-rest-api:<tag>`.
- **Keep the relay image patched** by rebuilding it regularly: `docker compose build --pull && docker compose up -d`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Relay exits with `Configuration error` | Required variable missing, or the token is weak |
| Still `Configuration error` after changing `docker-compose.yml` | The environment is only applied when the container is **created**: run `docker compose up -d` (not `restart`) |
| `401` in Sonarr/Radarr | Password ≠ `RELAY_TOKEN` |
| `Signal API error 400` in the log | Wrong `SIGNAL_NUMBER` or `SIGNAL_RECIPIENT` |
| `!request` gets no answer | Log must show `Signal receiver connected`; `signal-api` must run with `MODE=json-rpc` |
| Bot answers "not set up correctly" | Quality profile or root folder not found. The log lists the available ones |
| Group missing in `/v1/groups/…` | Wait a minute after linking, or run `docker compose restart signal-api` |

## License

[GNU General Public License v3.0](LICENSE). If you distribute modified versions, you must publish the source code under the same license.

