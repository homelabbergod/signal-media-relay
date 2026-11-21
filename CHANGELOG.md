# Changelog

## 0.9.2

### Changed
- **Less noise in the group:** a show is announced **once**, when Sonarr has nothing left for it in the download queue. Previously, episodes trickling in over hours produced several messages for the same show.
  - All new episodes of a show go into one message, across seasons ("Seasons 1, 2").
  - The relay asks Sonarr/Radarr only about that show/movie (`/queue/details?seriesId=` / `?movieId=`), so a large or stuck download of another title doesn't delay anything.
  - Stuck queue items (warning/error, e.g. import blocked) don't block an announcement.
  - After `ANNOUNCE_MAX_WAIT_SECONDS` (6 h) it is announced anyway.
  - If Sonarr/Radarr can't be reached, the relay announces instead of waiting.
- **Movies:** still one message per movie, but only once Radarr has finished with that movie.
- **`AGGREGATE_SECONDS` is now a quiet time:** each new episode of a show pushes the announcement back. Jellyfin-only setups (no Sonarr/Radarr access) rely on this alone.
- **Configuration:** new settings `ANNOUNCE_WAIT_FOR_QUEUE` (default `true`), `ANNOUNCE_MAX_WAIT_SECONDS` and `QUEUE_RECHECK_SECONDS`. The queue check only needs `SONARR_URL`/`RADARR_URL` plus API keys, `!request` doesn't have to be enabled.
- **`/health`:** also reports `pending_announcements`.

## 0.9.1

Result of a review with three perspectives (developer, DevSecOps, user). This release prepares the move to Unraid.

### Fixed
- **Announcements could stop silently:** one broken error response from the Signal API (connection dropped while reading it) killed the only sender thread. Every later message was lost while `/health` still said "ok". In a test against 0.9.0, only 15 of 90 messages arrived. Both background threads now survive any error.
- **`!release` limit bypass:** when the exact release wasn't found but similar ones were, the title stayed in Radarr/Sonarr (monitored) without counting towards the daily limit. It now counts, and the reply says the title was added.
- **Rare thread race** between the spam brake and the cleanup turned a command into "internal error". These now share a lock.

### Changed
- **Signal unreachable:** messages are no longer dropped after ~14 s. After 3 quick attempts they go into a retry list, retried every `SIGNAL_RETRY_INTERVAL` (60 s) for up to `SIGNAL_RETRY_WINDOW` (15 min). Permanent errors (wrong number/group) are still dropped right away.
- **Honest `/health`:** it returns **503** with a `problems` list when a background thread stopped, the Signal API has been unreachable for longer than `HEALTH_GRACE_SECONDS` (120 s), or the Signal receiver is disconnected. Docker/Unraid show the container as *unhealthy*.
- **Startup warning when `BOT_LANGUAGE` is not set:** 0.9.0 changed the default to English, and the warning avoids a silent language switch on upgrade.
- **Unraid template pinned to a version** (`:0.9.1` instead of `:latest`). Updates are deliberate.
- **signal-api isolated on a private network `signal-net`** shared only with the relay (compose and the Unraid guide). It has no authentication, so no other container may reach it. Resource limits were added (1 GB RAM, 512 PIDs; measured ~400 MB / ~160).
- **New Unraid walkthrough** in the README: browser-only setup, private network, gluetun firewall note, updating.
- **CI on GitHub Actions:** tests on Python 3.12 and 3.14, a version consistency check (`scripts/check_version.py`) and a test build of the image. Pushing a `v*` tag builds and pushes the image (with provenance and SBOM) and creates the GitHub release. Actions are pinned to commit SHAs.

## 0.9.0

### Changed
- **The whole project is now in English:** code, comments, log messages, README, Unraid template and tests.
- **Bot messages are configurable with `BOT_LANGUAGE=en|de`.** The default is **English**. To keep the German messages from 0.8.0, set `BOT_LANGUAGE=de`.
- English command aliases: `!help`, `!request movie|show <title>`, `… season 2`. The German forms (`!hilfe`, `film`/`serie`, `staffel 2`) keep working.

### Fixed
- Your own edited messages (sent from the bot's account) are now processed. The format was captured live from signal-cli-rest-api.
- Jellyfin "Generic Form" webhooks (form data instead of JSON) are accepted.

## 0.8.0 – first release

### Features
- Announcements from Sonarr, Radarr or Jellyfin in a Signal group: posters, episode bundling per season, duplicate protection.
- `!request <title>` for movies and shows. Shows ask which season (`0` = all). Downloads follow the quality profile.
- `!releases`, `!grab <n>` and `!release <release name>`: pick a specific indexer release. German release names are supported.
- Admin role (`REQUEST_ADMINS`): may grab releases rejected by the profile and has no daily limit.
- `!id`: sends your own Signal IDs privately, needed for admin/allow lists.
- Unraid template (`unraid/my-signal-media-relay.xml`).

### Fixed during the 0.8.0 audit
- **Large imports lost webhooks:** with `pids_limit=64`, the old one-timer-thread-per-show design dropped 10 of 70 webhooks (HTTP 500, "can't start new thread"). A single flusher thread now handles all of them.
- **Existing but unmonitored movies** were reported as "already requested and being searched". Radarr never searches those. They are now monitored again and searched.
- **Non-Latin titles** (e.g. Japanese) all normalised to an empty string. Two different films from the same year counted as duplicates and the second one was never announced.
- **Release names longer than 200 characters** were cut off, so `!release` could never find them.
- **Strangers sending DMs** got the spam-brake reply and could fill the command queue. DMs from non-members are now dropped before any processing.
- **Formatting characters in titles** (`*`, `~`, `` ` ``, `|`) broke Signal's bold/italic/spoiler formatting. They are now neutralised.
- **A stale season question** could catch the number from a later `!request` for another title.

### Edge cases now handled
- Edited messages, e.g. a typo fixed after sending.
- Commands written after an @mention.
- Replying with just a number, as a quote of the bot's list.
- Pasted release names with quotes, backticks or a file extension.
- Indexer titles with extra suffixes.
- Typos in release names: the bot offers similar releases.
- Shows that Sonarr only finds without the year.
- Release names without a season tag: falls back to Sonarr.
- Movies not released yet: the bot shows the expected date.
- Shows that haven't started yet.
- Configuration typos are rejected at startup.

### Known limitations
- Only one Radarr and one Sonarr instance (no separate 4K instance).
- Anime with absolute episode numbers: use `!releases <title> s<season>`; `!release` cannot resolve these.
- Specials (season 0) cannot be requested via the season question.
- Commands run one after another, so an indexer search (up to `RELEASE_SEARCH_TIMEOUT`) delays other commands.
- `bbernhard/signal-cli-rest-api:0.101` (latest at release time) has known CVEs that only its maintainer can fix. See README → Security.
