# melee-bot

A Discord bot that tracks players across [melee.gg](https://melee.gg) tournaments in real time. It posts a live embed in a channel that refreshes every minute while rounds are being played and highlights score changes as they come in.

## What it does

**Tournament bindings** (persistent, survive restarts):
- `/tournament bind <id or url>` — associate players from your roster with a tournament. Bindings are saved to SQLite so they persist across restarts. Binding also stores the tournament's name, start time and melee.gg status, all read from a single load of its page; every poll after that keeps the start time and status up to date from the same page load that lists the rounds.
- `/tournament unbind <id or url>` — remove a tournament and its player bindings when you're done with it.
- `/tournament list` — show all bound tournaments, their players, and whether they've concluded.

**Watching:**
- `/watch start` — if you have active bindings, shows a dropdown to pick which tournaments to watch and starts the live embed. Falls back to a manual entry modal if no bindings exist.
- `/watch status` — shows a win/loss/draw summary for all tracked players without affecting the live embed.
- `/watch stop` — stops polling and marks the embed as concluded.

**Roster:**
- `/player add/remove/list` — manage the player roster stored in SQLite. Players in the roster appear as options when binding tournaments.

**Discovery** (needs the *Manage Server* permission):
- `/discovery channel [channel]` — turn discovery on for this server and choose the channel that tournament suggestions are posted in (the current channel if none is given).
- `/discovery off` — turn it off again.

With discovery on, the bot looks up every roster player's tournaments on melee.gg every 3 hours (and once at startup) and posts a suggestion for each tournament that is ongoing or starts within 7 days, unless the server has already bound it or been shown it before. There is one message per tournament, listing which of that server's players are entered, with two buttons:

- **Bind & watch** — binds the tournament with those players and starts a watch in the discovery channel. If the tournament hasn't started, the watch simply waits (polling every 15 minutes) until round 1 is paired. If that channel already has a watch, the tournament is added to it, up to the limit of 4; beyond that it is still bound and can be started later with `/watch start`.
- **Ignore** — dismisses the suggestion; it won't be posted again.

Both buttons need the *Manage Server* permission and keep working after the bot restarts. A few things to know:

- Discovery looks players up by **exact melee.gg username**. Live tracking matches roster names loosely (a roster entry that is only part of a username still works there), but such an entry will never produce a suggestion.
- A player on several servers' rosters is looked up once per scan, and scans run at the lowest priority, behind all live `/watch` and `/topcut` fetches.
- Tournaments that started more than 30 days ago are not suggested even if melee.gg never marked them as ended.

Rosters, bindings and their settings are stored per Discord server: the same melee.gg username can sit on two servers' rosters with a different display name in each, and each server binds, watches and concludes tournaments independently.

**Automatic behaviour:**
- A tournament concludes for a server when melee.gg shows it as **Ended**, or after **5 days with no changes** (for events that are abandoned and never reach "Ended"). It is then marked concluded for that server only and won't appear in its future `/watch start` selectors; another server watching the same tournament keeps its own state. A watch ends once every tournament in it has concluded, or when someone runs `/watch stop`.
- The per-server `rounds_to_track` setting (`/tournament bind`, `/tournament settings`) is still stored but no longer decides when a tournament concludes.
- Watches run across days — there is no nightly reset. Each tournament is polled every 60 seconds while it is **Active** and every 15 minutes once it goes **Idle**: about 90 minutes with no changes and no tracked player still waiting on a result (between days of a multi-day event, say), or straight away if round 1 hasn't started yet. It becomes Active again as soon as a new round is paired or anything else changes.
- While every tournament in a watch is Idle, its embed footer shows "💤 Waiting for the next round". If the next change comes more than 6 hours after the previous one, the bot posts a fresh embed and marks the old one "Continued below ⬇", so the live embed isn't left far up the channel.
- On restart, any active watch sessions automatically resume in the same channel and edit the existing embed. Resumed watches start out Active and settle back to Idle on their own.
- A tournament is fetched from melee.gg once per poll no matter how many channels or servers are watching it: the bot fetches it for the combined player list and each watch shows only its own players. Starting a watch on a tournament that is already being polled reuses the latest result instead of fetching again.

Changes since the last poll are highlighted inline:

```
🟢 mossgoblin  — Round 3 vs QuietRook: 2-1 ✅
🔴 NovaTide    — Rank 4 → 7 ▼
```

Up to 4 tournaments can be watched simultaneously per channel (one active watch per channel).

---

## Typical workflow

```
# One-time roster setup
/player add Kestrel_VX
/player add mossgoblin
/player add NovaTide

# Before or during a tournament day
/tournament bind 430190    →  select players from roster  →  Save Binding
/tournament bind 430189    →  select players from roster  →  Save Binding

# Start watching (select which tournaments from the dropdown)
/watch start

# If the bot restarts, it automatically resumes — no action needed.

# Or let the bot find tournaments for you: turn discovery on once, then press
# "Bind & watch" on the suggestions it posts.
/discovery channel #tournaments

# When melee.gg shows a tournament as Ended it is marked concluded automatically.
# Once you're fully done with it:
/tournament unbind 430190
```

---

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- A Discord bot token (see [Discord setup](#discord-setup))

---

## Local setup

```bash
git clone <your-repo-url>
cd melee-bot

cp .env.example .env
# Fill in DISCORD_TOKEN and GUILD_IDS (see Discord setup below)

uv sync
uv run python bot.py
```

The bot creates `melee.db` in the current directory on first run. Set `DB_PATH` in `.env` to change the location.

### Database upgrades

The schema version is kept in SQLite's `PRAGMA user_version` and upgraded automatically on start. Before upgrading a database that already holds data, the bot copies it to `<DB_PATH>.v<old version>.bak` next to the original.

Upgrading a database from before multi-server support (version 0) assigns every existing player, binding and session to the server in `GUILD_ID` — or, if that is no longer set, to the first server listed in `GUILD_IDS`. Make sure that is your original server on the first start after updating.

---

## Discord setup

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications) → **New Application**
2. **Bot** tab → **Reset Token** → copy the token into `DISCORD_TOKEN` in `.env`
3. **Bot** tab → disable all Privileged Gateway Intents
4. **Bot** tab → disable **Public Bot** to keep it private
5. **Installation** tab → set **Install Link** to **None**
6. **OAuth2 → URL Generator** → scopes: `bot` + `applications.commands`, permissions: `Send Messages`, `Embed Links`, `Read Message History`, `View Channels`
7. Open the generated URL and invite the bot to your server
8. Right-click the server icon → **Copy Server ID** (requires Developer Mode in Discord settings) → paste into `GUILD_IDS` in `.env`

### Adding another server

Repeat steps 6–8 for the new server and append its ID to `GUILD_IDS` (comma-separated, e.g. `GUILD_IDS=123,456`), then restart the bot. The new server starts with an empty roster and no bindings; nothing is shared with the existing one except the cached facts about each tournament. Commands only work inside those servers — they are not available in DMs.

---

## Running tests

```bash
uv sync --group dev
uv run pytest
```

Tests cover the melee.gg API parsing and diff logic (`test_tracker.py`, `test_melee_client.py`), the shared per-tournament polling, concluding and Active/Idle behaviour (`test_feed.py`), tournament discovery (`test_discovery.py`), and the database layer including schema migrations (`test_db.py`, `test_db_migration.py`). HTTP calls are mocked so no network access is needed.

---

## Demo mode

`demo_fake_melee/` is a fake melee.gg that replays the captured tournaments in `tests/fixtures/` step by step, with invented player names, so the bot can be shown working (and screenshotted) in a pair of demo servers without a live event. See [demo_fake_melee/README.md](demo_fake_melee/README.md).

---

## Deployment

The production image is built from the `Dockerfile` and deployed from a separate private repository. This repo's CI only lints and tests.

---

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `DISCORD_TOKEN` | Yes | — | Bot token from Discord Developer Portal |
| `GUILD_IDS` | Yes | — | Comma-separated Discord server IDs to register slash commands in |
| `GUILD_ID` | No | — | Older single-server setting; used when `GUILD_IDS` is unset |
| `DB_PATH` | No | `melee.db` | Path to the SQLite database |
| `LOG_LEVEL` | No | `INFO` | Log verbosity (`DEBUG` logs every HTTP request) |

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
