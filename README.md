# Signal Media Relay

**Version 0.9.2** · [Changelog](CHANGELOG.md) · Docker image: [`homelabbergod/signal-media-relay`](https://hub.docker.com/r/homelabbergod/signal-media-relay)

Announces new movies and episodes from **Sonarr/Radarr** (or **Jellyfin**) in a **Signal group**. Group members can request movies and shows right from the chat.

```
🎬 New movie: Example Movie (2024)            📺 3 new episodes: Example Show – Season 2
Quality: Bluray-1080p                         S02E01 – Episode One
🙋 Requested by Anna             [poster]     S02E02 – Episode Two …            [poster]
```

- One message per show instead of one per episode: it waits until Sonarr/Radarr finished downloading that show or movie. Posters included, no duplicates.
- `!request <title>` in the chat adds movies and shows to Radarr/Sonarr. You can also pick a specific release.
- Bot messages in English or German (`BOT_LANGUAGE=en|de`).
- A single Python file with no dependencies, running in a hardened container (non-root, read-only).

```
Sonarr / Radarr / Jellyfin ──webhook──▶ relay :8090 ──▶ signal-cli-rest-api ──▶ Signal group
```

## Quick start (Docker Compose)

You need Docker and a phone number for the bot. A dedicated number is recommended; see [Security](#security).

```bash
git clone https://github.com/homelabbergod/signal-media-relay.git && cd signal-media-relay
docker compose up -d signal-api
```

1. **Link the bot account:** open `http://localhost:8080/v1/qrcodelink?device_name=media-relay` and scan the code in Signal under *Settings → Linked devices*.
2. **Create a group** with the bot in it. Then open `http://localhost:8080/v1/groups/<bot-number>` and copy the `"id"` (`group.…`).
3. **Configure** the `environment:` block in `docker-compose.yml`. Every option is commented there. At least set:
   ```yaml
   SIGNAL_NUMBER: "+4900000000000"
   SIGNAL_RECIPIENT: "group.…"
   RELAY_TOKEN: "…"        # python -c "import secrets;print(secrets.token_urlsafe(24))"
   BOT_LANGUAGE: "en"      # or "de"
   ```
4. **Start the relay:** run `docker compose up -d --build`. Then `http://localhost:8090/health` should show `"status": "ok"`.

> Signal has no broadcast channels. To make the group act like one, set *Group settings → Send messages → Only admins*. Members can still use the bot via direct message.

## Unraid

1. **Private network:** run `docker network create signal-net` in the Unraid terminal. Then set *Settings → Docker → Preserve user defined networks* to **Yes**.
2. **Signal API container:**

   | Setting | Value |
   |---|---|
   | Name | `signal-api` |
   | Repository | `bbernhard/signal-cli-rest-api:0.101` |
   | Network | `signal-net` |
   | `MODE` | `json-rpc` |
   | Path | `/home/.local/share/signal-cli` → `/mnt/user/appdata/signal-cli-rest-api` |
   | Port | `8080` (temporary, for setup only) |

   Link the account and get the group ID as in steps 1–2 above, using `http://<unraid-ip>:<port>`. **Then remove the port.**
3. **Relay container:** copy [`unraid/my-signal-media-relay.xml`](unraid/my-signal-media-relay.xml) to `/boot/config/plugins/dockerMan/templates-user/`. Then go to *Docker → Add Container* and pick the template. The network and hardening options are preset; fill in the values.
4. **Updates:** the template uses a fixed version. To update, change the tag in *Repository* after reading the [Changelog](CHANGELOG.md).

**Radarr/Sonarr behind gluetun:**
- **Relay → Radarr/Sonarr:** use `http://<unraid-ip>:7878` and `:8989`.
- **Radarr/Sonarr → relay (webhooks):** use `http://<unraid-ip>:8090/radarr` and `/sonarr`. gluetun must allow your LAN: set `FIREWALL_OUTBOUND_SUBNETS=192.168.1.0/24`, adjusted to your network.

## Connect a source

**Sonarr / Radarr** (recommended): *Settings → Connect → + → Webhook*

| Field | Value |
|---|---|
| Triggers | On File Import (optionally On File Upgrade) |
| URL | `http://<relay-host>:8090/sonarr`, or `/radarr` for Radarr |
| Username / Password | `relay` / your `RELAY_TOKEN` |

**Jellyfin** (alternative): install the *Webhook* plugin and add a *Generic* (or *Generic Form*) destination.

| Field | Value |
|---|---|
| URL | `http://<relay-host>:8090/jellyfin` |
| Notification type / Item type | Item Added / Movies, Episodes |
| Template | [`jellyfin-template.json`](jellyfin-template.json) |
| Header | `X-Relay-Token: <RELAY_TOKEN>` |

For Jellyfin posters and descriptions, also set `JELLYFIN_URL` and `JELLYFIN_API_KEY`. Use one source per library.

## Requests from the chat

To turn this on, set `REQUESTS_ENABLED: "true"` plus `RADARR_URL`/`RADARR_API_KEY` and/or `SONARR_URL`/`SONARR_API_KEY`. The API key is under *Settings → General → Security*.

```
Ben:  !request Example Show
Bot:  📺 Example Show (2022) – which season?
      0. All seasons
      1. Season 1 (9 episodes) ✔️ available
      2. Season 2 (10 episodes)
Ben:  2                      (reply to the list, or: !request 2)
Bot:  ✅ Requested: 📺 Example Show (2022) – season 2
```

| Command | What it does |
|---|---|
| `!request <title>` | Adds a movie, or asks which season for a show. Downloads follow your quality profile. Add a year or `s2` to be precise. |
| `!releases <title>` → `!grab <no.>` | Pick a specific release from the indexers, like *Interactive Search* |
| `!release <release name>` | Download exactly this release. The title is detected from the name, German release names included. |
| `!id` | Sends you your Signal ID privately, for the admin and allow lists |
| `!help` / `!hilfe` | Help |

The bot replies to each request with a quote. You can also reply to a bot list with just the number, and edited messages count too.

**Protection built in:**
- a daily limit per person (`REQUEST_LIMIT_PER_DAY`, default 5 titles) and a spam brake
- only group members can make requests, and DMs from strangers are ignored
- members only get releases your quality profile accepts; `REQUEST_ADMINS` may override that and have no limit
- the bot only adds titles and never deletes your files

## Configuration

All options are commented in [`docker-compose.yml`](docker-compose.yml) and in the Unraid template. The most important ones:

| Variable | Default | Purpose |
|---|---|---|
| `SIGNAL_NUMBER`, `SIGNAL_RECIPIENT`, `RELAY_TOKEN` | – | Required (see quick start) |
| `BOT_LANGUAGE` | `en` | `en` or `de` |
| `ANNOUNCE_TESTS` | `false` | Post the *Test* button of Sonarr/Radarr into the group |
| `ANNOUNCE_WAIT_FOR_QUEUE` | `true` | Announce a show/movie only when Sonarr/Radarr has nothing left for it in the download queue (needs `SONARR_URL`/`RADARR_URL` + API key; max. `ANNOUNCE_MAX_WAIT_SECONDS`, default 6 h) |
| `REQUESTS_ENABLED` | `false` | Turn on chat requests |
| `REQUEST_LIMIT_PER_DAY` | `5` | Titles per person per 24 h (`0` = unlimited) |
| `REQUEST_ADMINS` | – | Numbers/UUIDs (from `!id`) with extra rights |

Changes take effect with `docker compose up -d`. A plain `restart` keeps the old values.

## Security

- **Keep `signal-cli-rest-api` on a private network that only the relay joins**, and don't publish its port after setup. It has **no authentication**: whoever can reach it can read and send all messages of the linked account.
- **Use a dedicated number for the bot** if you can. If you link your personal account, the bot sees your private messages, and its replies come from you.
- **Keep the relay in your LAN.** It speaks plain HTTP; put a TLS reverse proxy in front if it has to leave the LAN.
- **`signal-cli-rest-api:0.101` has known CVEs** that only its maintainer can fix. Update it when new releases appear.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Configuration error` at start | A required value is missing or wrong. The log says which one. |
| `/health` returns 503 | It lists the `problems`, e.g. Signal API unreachable. Messages are retried for 15 min. |
| `401` in Sonarr/Radarr | The password is not your `RELAY_TOKEN` |
| *Test* fails behind gluetun | Set `FIREWALL_OUTBOUND_SUBNETS` on gluetun to your LAN |
| `!request` gets no answer | The log must show `Signal receiver connected`, and signal-api needs `MODE=json-rpc` |
| "not set up correctly" | The quality profile or root folder was not found. The log lists the available ones. |

## Development

`python tests/smoketest.py` and `python tests/test_requests.py` run the relay against fake Signal and Radarr/Sonarr servers. You need no Docker and no accounts. CI runs both on every push.

## License

[GPL-3.0](LICENSE)
