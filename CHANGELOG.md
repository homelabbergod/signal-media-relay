# Changelog

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
