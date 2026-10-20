import datetime
import json
import logging
import os
import shutil

import aiosqlite

log = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "melee.db")


# ── schema & migrations ────────────────────────────────────────────────────────
# The schema version lives in SQLite's PRAGMA user_version. Each entry in
# _MIGRATIONS takes the database from version N to N+1 (its index is N); a
# brand-new file starts at 0 and runs them all. Before migrating a file that
# already holds data, init_db copies it to "<DB_PATH>.v<N>.bak".

async def _table_exists(conn: aiosqlite.Connection, name: str) -> bool:
    cur = await conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    )
    return await cur.fetchone() is not None


async def _columns(conn: aiosqlite.Connection, table: str) -> set[str]:
    cur = await conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cur.fetchall()}


async def _migrate_to_v1(conn: aiosqlite.Connection, legacy_guild_id: int | None) -> None:
    """Per-server rosters and bindings.

    `tournaments` keeps only facts about the event itself; each server's
    settings move to `guild_tournaments`, and the roster tables gain guild_id.
    Rows from the single-server schema are assigned to legacy_guild_id.
    """
    legacy = {
        t: await _table_exists(conn, t)
        for t in ("players", "tournaments", "tournament_players", "watch_sessions", "topcut_sessions")
    }
    if any(legacy.values()) and legacy_guild_id is None:
        raise RuntimeError(
            "This database predates multi-server support; a guild ID is needed to "
            "assign its existing rows to (set GUILD_ID)."
        )
    old_cols = {t: await _columns(conn, t) for t, exists in legacy.items() if exists}
    for t in ("players", "tournaments", "tournament_players"):
        if legacy[t]:
            await conn.execute(f"ALTER TABLE {t} RENAME TO {t}_v0")

    await conn.execute("""
        CREATE TABLE players (
            guild_id       INTEGER NOT NULL,
            name           TEXT NOT NULL,
            added_by       TEXT NOT NULL,
            added_at       TEXT NOT NULL DEFAULT (datetime('now')),
            displayed_name TEXT,
            PRIMARY KEY (guild_id, name)
        )
    """)
    await conn.execute("""
        CREATE TABLE tournaments (
            tournament_id INTEGER PRIMARY KEY,
            name          TEXT,
            start_time    TEXT,
            status        TEXT
        )
    """)
    await conn.execute("""
        CREATE TABLE guild_tournaments (
            guild_id        INTEGER NOT NULL,
            tournament_id   INTEGER NOT NULL,
            concluded       INTEGER NOT NULL DEFAULT 0,
            rounds_to_track INTEGER,
            added_by        TEXT NOT NULL,
            added_at        TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (guild_id, tournament_id)
        )
    """)
    await conn.execute("""
        CREATE TABLE tournament_players (
            guild_id      INTEGER NOT NULL,
            tournament_id INTEGER NOT NULL,
            player_name   TEXT NOT NULL,
            PRIMARY KEY (guild_id, tournament_id, player_name)
        )
    """)

    if legacy["players"]:
        displayed = "displayed_name" if "displayed_name" in old_cols["players"] else "NULL"
        await conn.execute(
            "INSERT INTO players (guild_id, name, added_by, added_at, displayed_name) "
            f"SELECT ?, name, added_by, added_at, {displayed} FROM players_v0",
            (legacy_guild_id,),
        )
        await conn.execute("DROP TABLE players_v0")
    if legacy["tournaments"]:
        name = "name" if "name" in old_cols["tournaments"] else "NULL"
        rounds = "rounds_to_track" if "rounds_to_track" in old_cols["tournaments"] else "NULL"
        await conn.execute(
            f"INSERT INTO tournaments (tournament_id, name) SELECT tournament_id, {name} FROM tournaments_v0"
        )
        await conn.execute(
            "INSERT INTO guild_tournaments "
            "(guild_id, tournament_id, concluded, rounds_to_track, added_by, added_at) "
            f"SELECT ?, tournament_id, concluded, {rounds}, added_by, added_at FROM tournaments_v0",
            (legacy_guild_id,),
        )
        await conn.execute("DROP TABLE tournaments_v0")
    if legacy["tournament_players"]:
        await conn.execute(
            "INSERT INTO tournament_players (guild_id, tournament_id, player_name) "
            "SELECT ?, tournament_id, player_name FROM tournament_players_v0",
            (legacy_guild_id,),
        )
        await conn.execute("DROP TABLE tournament_players_v0")

    # Sessions stay keyed by channel (a channel belongs to exactly one server),
    # so these only need the extra column.
    for t in ("watch_sessions", "topcut_sessions"):
        if legacy[t]:
            await conn.execute(f"ALTER TABLE {t} ADD COLUMN guild_id INTEGER NOT NULL DEFAULT 0")
            await conn.execute(f"UPDATE {t} SET guild_id = ?", (legacy_guild_id,))
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS watch_sessions (
            channel_id     INTEGER PRIMARY KEY,
            message_id     INTEGER NOT NULL,
            tournament_ids TEXT NOT NULL,
            guild_id       INTEGER NOT NULL DEFAULT 0
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS topcut_sessions (
            channel_id    INTEGER PRIMARY KEY,
            message_id    INTEGER NOT NULL,
            tournament_id INTEGER NOT NULL,
            cut_size      INTEGER NOT NULL,
            started_by    TEXT NOT NULL,
            started_at    TEXT NOT NULL DEFAULT (datetime('now')),
            guild_id      INTEGER NOT NULL DEFAULT 0
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS round_results (
            tournament_id INTEGER NOT NULL,
            player        TEXT NOT NULL,
            round_name    TEXT NOT NULL,
            result        TEXT NOT NULL,
            opponent      TEXT NOT NULL,
            PRIMARY KEY (tournament_id, player, round_name)
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS round_match_cache (
            tournament_id INTEGER NOT NULL,
            round_id      INTEGER NOT NULL,
            matches_json  TEXT NOT NULL,
            PRIMARY KEY (tournament_id, round_id)
        )
    """)


async def _migrate_to_v2(conn: aiosqlite.Connection, legacy_guild_id: int | None) -> None:
    """When the bot last saw anything change in a tournament — the clock for
    concluding a watch after 5 days of silence, which has to survive restarts."""
    await conn.execute("ALTER TABLE tournaments ADD COLUMN last_change_at TEXT")


async def _migrate_to_v3(conn: aiosqlite.Connection, legacy_guild_id: int | None) -> None:
    """Tournament discovery: where each server wants suggestions posted, and
    which tournaments it has already been shown."""
    await conn.execute("""
        CREATE TABLE guild_settings (
            guild_id             INTEGER PRIMARY KEY,
            discovery_channel_id INTEGER
        )
    """)
    await conn.execute("""
        CREATE TABLE discovery_suggestions (
            guild_id      INTEGER NOT NULL,
            tournament_id INTEGER NOT NULL,
            status        TEXT NOT NULL DEFAULT 'pending',
            message_id    INTEGER,
            players       TEXT NOT NULL DEFAULT '[]',
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (guild_id, tournament_id)
        )
    """)


_MIGRATIONS = [_migrate_to_v1, _migrate_to_v2, _migrate_to_v3]
SCHEMA_VERSION = len(_MIGRATIONS)


async def init_db(legacy_guild_id: int | None = None) -> None:
    """Create or upgrade the schema.

    legacy_guild_id is the server that rows from the old single-server schema
    are assigned to; it is only needed when such a database is being upgraded.
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("PRAGMA user_version")
        version = (await cur.fetchone())[0]
        cur = await conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'")
        has_data = (await cur.fetchone())[0] > 0
    if version >= SCHEMA_VERSION:
        return
    if has_data:
        backup = f"{DB_PATH}.v{version}.bak"
        shutil.copy2(DB_PATH, backup)
        log.info("Backed up %s to %s before migrating v%s → v%s", DB_PATH, backup, version, SCHEMA_VERSION)

    # isolation_level=None: we manage the transaction ourselves so that each
    # migration's DDL and its version bump commit (or roll back) together.
    async with aiosqlite.connect(DB_PATH, isolation_level=None) as conn:
        for target in range(version + 1, SCHEMA_VERSION + 1):
            await conn.execute("BEGIN")
            try:
                await _MIGRATIONS[target - 1](conn, legacy_guild_id)
                await conn.execute(f"PRAGMA user_version = {target}")
                await conn.execute("COMMIT")
            except BaseException:
                await conn.execute("ROLLBACK")
                raise


# ── roster ─────────────────────────────────────────────────────────────────────

async def add_player(
    guild_id: int, name: str, added_by: str, displayed_name: str | None = None
) -> bool:
    async with aiosqlite.connect(DB_PATH) as conn:
        try:
            await conn.execute(
                "INSERT INTO players (guild_id, name, added_by, displayed_name) VALUES (?, ?, ?, ?)",
                (guild_id, name, added_by, displayed_name),
            )
            await conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def remove_player(guild_id: int, name: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "DELETE FROM players WHERE guild_id = ? AND name = ?", (guild_id, name)
        )
        await conn.commit()
        return cur.rowcount > 0


async def set_player_display_name(guild_id: int, name: str, displayed_name: str | None) -> bool:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "UPDATE players SET displayed_name = ? WHERE guild_id = ? AND name = ?",
            (displayed_name, guild_id, name),
        )
        await conn.commit()
        return cur.rowcount > 0


async def get_display_names(guild_id: int) -> dict[str, str]:
    """Return {melee_name: displayed_name} for this server's players that have one set."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT name, displayed_name FROM players "
            "WHERE guild_id = ? AND displayed_name IS NOT NULL",
            (guild_id,),
        )
        rows = await cur.fetchall()
        return {r[0]: r[1] for r in rows}


async def list_players(guild_id: int) -> list[str]:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT name FROM players WHERE guild_id = ? ORDER BY name", (guild_id,)
        )
        rows = await cur.fetchall()
        return [r[0] for r in rows]


# ── tournament bindings ────────────────────────────────────────────────────────
# `tournaments` rows describe the event and are shared; everything a server
# chooses for itself (players, rounds_to_track, concluded) is keyed by guild_id.

async def add_tournament(
    guild_id: int,
    tournament_id: int,
    added_by: str,
    rounds_to_track: int | None = None,
    name: str | None = None,
) -> bool:
    """Bind a tournament to a server; no-op (returns False) if already bound there."""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT INTO tournaments (tournament_id, name) VALUES (?, ?) "
            "ON CONFLICT(tournament_id) DO UPDATE SET name = COALESCE(tournaments.name, excluded.name)",
            (tournament_id, name),
        )
        try:
            await conn.execute(
                "INSERT INTO guild_tournaments (guild_id, tournament_id, added_by, rounds_to_track) "
                "VALUES (?, ?, ?, ?)",
                (guild_id, tournament_id, added_by, rounds_to_track),
            )
            inserted = True
        except aiosqlite.IntegrityError:
            inserted = False
        await conn.commit()
        return inserted


async def get_tournament_name(tournament_id: int) -> str | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT name FROM tournaments WHERE tournament_id = ?", (tournament_id,)
        )
        row = await cur.fetchone()
        return row[0] if row else None


async def set_tournament_name(tournament_id: int, name: str | None) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE tournaments SET name = ? WHERE tournament_id = ?",
            (name, tournament_id),
        )
        await conn.commit()


async def set_tournament_info(
    tournament_id: int, start_time: datetime.datetime | None, status: str | None
) -> None:
    """Store what the tournament's page says about the event. A None leaves
    the stored value alone, so a page that fails to show one doesn't erase it."""
    start = start_time.astimezone(datetime.UTC).isoformat() if start_time else None
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE tournaments SET start_time = COALESCE(?, start_time), status = COALESCE(?, status) "
            "WHERE tournament_id = ?",
            (start, status, tournament_id),
        )
        await conn.commit()


async def get_tournament_info(tournament_id: int) -> dict | None:
    """The shared facts about an event: {"name", "start_time", "status"}."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT name, start_time, status FROM tournaments WHERE tournament_id = ?",
            (tournament_id,),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    name, start_time, status = row
    return {
        "name": name,
        "start_time": datetime.datetime.fromisoformat(start_time) if start_time else None,
        "status": status,
    }


async def set_tournament_last_change(tournament_id: int, when: datetime.datetime) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE tournaments SET last_change_at = ? WHERE tournament_id = ?",
            (when.astimezone(datetime.UTC).isoformat(), tournament_id),
        )
        await conn.commit()


async def get_tournament_last_change(tournament_id: int) -> datetime.datetime | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT last_change_at FROM tournaments WHERE tournament_id = ?", (tournament_id,)
        )
        row = await cur.fetchone()
    return datetime.datetime.fromisoformat(row[0]) if row and row[0] else None


async def set_tournament_rounds_to_track(
    guild_id: int, tournament_id: int, rounds_to_track: int | None
) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE guild_tournaments SET rounds_to_track = ? WHERE guild_id = ? AND tournament_id = ?",
            (rounds_to_track, guild_id, tournament_id),
        )
        await conn.commit()


async def get_tournament_rounds_to_track(guild_id: int, tournament_id: int) -> int | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT rounds_to_track FROM guild_tournaments WHERE guild_id = ? AND tournament_id = ?",
            (guild_id, tournament_id),
        )
        row = await cur.fetchone()
        return row[0] if row else None


async def remove_tournament(guild_id: int, tournament_id: int) -> bool:
    """Unbind a tournament from one server. The shared `tournaments` row goes
    too once no server is bound to it any more."""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "DELETE FROM tournament_players WHERE guild_id = ? AND tournament_id = ?",
            (guild_id, tournament_id),
        )
        cur = await conn.execute(
            "DELETE FROM guild_tournaments WHERE guild_id = ? AND tournament_id = ?",
            (guild_id, tournament_id),
        )
        await conn.execute(
            "DELETE FROM tournaments WHERE tournament_id = ? "
            "AND NOT EXISTS (SELECT 1 FROM guild_tournaments WHERE tournament_id = ?)",
            (tournament_id, tournament_id),
        )
        await conn.commit()
        return cur.rowcount > 0


async def set_tournament_concluded(guild_id: int, tournament_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE guild_tournaments SET concluded = 1 WHERE guild_id = ? AND tournament_id = ?",
            (guild_id, tournament_id),
        )
        await conn.commit()


async def set_tournament_players(guild_id: int, tournament_id: int, players: list[str]) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "DELETE FROM tournament_players WHERE guild_id = ? AND tournament_id = ?",
            (guild_id, tournament_id),
        )
        await conn.executemany(
            "INSERT INTO tournament_players (guild_id, tournament_id, player_name) VALUES (?, ?, ?)",
            [(guild_id, tournament_id, p) for p in players],
        )
        await conn.commit()


async def get_tournament_players(guild_id: int, tournament_id: int) -> list[str]:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT player_name FROM tournament_players "
            "WHERE guild_id = ? AND tournament_id = ? ORDER BY player_name",
            (guild_id, tournament_id),
        )
        rows = await cur.fetchall()
        return [r[0] for r in rows]


async def list_tournaments(guild_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT gt.tournament_id, gt.concluded, gt.rounds_to_track, t.name, t.start_time, t.status "
            "FROM guild_tournaments gt LEFT JOIN tournaments t USING (tournament_id) "
            "WHERE gt.guild_id = ? ORDER BY gt.tournament_id",
            (guild_id,),
        )
        rows = await cur.fetchall()
        result = []
        for tournament_id, concluded, rounds_to_track, name, start_time, status in rows:
            pcur = await conn.execute(
                "SELECT player_name FROM tournament_players "
                "WHERE guild_id = ? AND tournament_id = ? ORDER BY player_name",
                (guild_id, tournament_id),
            )
            players = [r[0] for r in await pcur.fetchall()]
            result.append({
                "id": tournament_id,
                "concluded": bool(concluded),
                "players": players,
                "rounds_to_track": rounds_to_track,
                "name": name,
                "start_time": datetime.datetime.fromisoformat(start_time) if start_time else None,
                "status": status,
            })
        return result


# ── watch session persistence ──────────────────────────────────────────────────

async def save_watch_session(
    guild_id: int, channel_id: int, message_id: int, tournament_ids: list[int]
) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """INSERT INTO watch_sessions (guild_id, channel_id, message_id, tournament_ids)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(channel_id) DO UPDATE
               SET guild_id = excluded.guild_id,
                   message_id = excluded.message_id,
                   tournament_ids = excluded.tournament_ids""",
            (guild_id, channel_id, message_id, json.dumps(tournament_ids)),
        )
        await conn.commit()


async def update_watch_session_message(channel_id: int, message_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE watch_sessions SET message_id = ? WHERE channel_id = ?",
            (message_id, channel_id),
        )
        await conn.commit()


async def delete_watch_session(channel_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("DELETE FROM watch_sessions WHERE channel_id = ?", (channel_id,))
        await conn.commit()


async def load_watch_sessions() -> list[tuple[int, int, int, list[int]]]:
    """Returns [(guild_id, channel_id, message_id, tournament_ids), ...]."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT guild_id, channel_id, message_id, tournament_ids FROM watch_sessions"
        )
        rows = await cur.fetchall()
        return [(r[0], r[1], r[2], json.loads(r[3])) for r in rows]


# ── round result cache ─────────────────────────────────────────────────────────

# Type alias used by tracker.py: {player: {round_name: (result, opponent)}}
RoundCache = dict[str, dict[str, tuple[str, str]]]


async def save_round_results(tournament_id: int, data: RoundCache) -> None:
    """Upsert finalized round results for a tournament."""
    async with aiosqlite.connect(DB_PATH) as conn:
        for player, rounds in data.items():
            for round_name, (result, opponent) in rounds.items():
                await conn.execute(
                    """INSERT INTO round_results (tournament_id, player, round_name, result, opponent)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(tournament_id, player, round_name) DO UPDATE
                       SET result = excluded.result, opponent = excluded.opponent""",
                    (tournament_id, player, round_name, result, opponent),
                )
        await conn.commit()


async def load_round_results(tournament_id: int) -> RoundCache:
    """Return all cached finalized round results for a tournament."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT player, round_name, result, opponent FROM round_results WHERE tournament_id = ?",
            (tournament_id,),
        )
        rows = await cur.fetchall()
    cache: RoundCache = {}
    for player, round_name, result, opponent in rows:
        cache.setdefault(player, {})[round_name] = (result, opponent)
    return cache


# ── topcut session persistence ─────────────────────────────────────────────────

async def save_topcut_session(
    guild_id: int, channel_id: int, message_id: int, tournament_id: int, cut_size: int, started_by: str,
) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """INSERT INTO topcut_sessions
                   (guild_id, channel_id, message_id, tournament_id, cut_size, started_by)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(channel_id) DO UPDATE
               SET guild_id = excluded.guild_id,
                   message_id = excluded.message_id,
                   tournament_id = excluded.tournament_id,
                   cut_size = excluded.cut_size""",
            (guild_id, channel_id, message_id, tournament_id, cut_size, started_by),
        )
        await conn.commit()


async def update_topcut_session_message(channel_id: int, message_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE topcut_sessions SET message_id = ? WHERE channel_id = ?",
            (message_id, channel_id),
        )
        await conn.commit()


async def delete_topcut_session(channel_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("DELETE FROM topcut_sessions WHERE channel_id = ?", (channel_id,))
        await conn.commit()


async def load_topcut_sessions() -> list[tuple[int, int, int, int, int, str]]:
    """Returns [(guild_id, channel_id, message_id, tournament_id, cut_size, started_by), ...]."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT guild_id, channel_id, message_id, tournament_id, cut_size, started_by "
            "FROM topcut_sessions"
        )
        return list(await cur.fetchall())


# ── round match cache (for the topcut rematch graph) ───────────────────────────

async def save_round_match_cache(tournament_id: int, round_id: int, matches: list[dict]) -> None:
    """Cache a completed round's raw match rows — they never change once the
    round is over, so this avoids re-fetching them every poll."""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """INSERT INTO round_match_cache (tournament_id, round_id, matches_json)
               VALUES (?, ?, ?)
               ON CONFLICT(tournament_id, round_id) DO UPDATE SET matches_json = excluded.matches_json""",
            (tournament_id, round_id, json.dumps(matches)),
        )
        await conn.commit()


async def load_round_match_cache(tournament_id: int) -> dict[int, list[dict]]:
    """Returns {round_id: raw match rows} for every cached round of a tournament."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT round_id, matches_json FROM round_match_cache WHERE tournament_id = ?",
            (tournament_id,),
        )
        rows = await cur.fetchall()
        return {round_id: json.loads(matches_json) for round_id, matches_json in rows}


# ── discovery ──────────────────────────────────────────────────────────────────

SUGGESTION_PENDING = "pending"
SUGGESTION_BOUND = "bound"
SUGGESTION_IGNORED = "ignored"


async def set_discovery_channel(guild_id: int, channel_id: int | None) -> None:
    """Choose where a server's suggestions go; None turns discovery off."""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """INSERT INTO guild_settings (guild_id, discovery_channel_id) VALUES (?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET discovery_channel_id = excluded.discovery_channel_id""",
            (guild_id, channel_id),
        )
        await conn.commit()


async def get_discovery_channel(guild_id: int) -> int | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT discovery_channel_id FROM guild_settings WHERE guild_id = ?", (guild_id,)
        )
        row = await cur.fetchone()
        return row[0] if row else None


async def list_discovery_channels() -> dict[int, int]:
    """{guild_id: channel_id} for every server with discovery turned on."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT guild_id, discovery_channel_id FROM guild_settings "
            "WHERE discovery_channel_id IS NOT NULL"
        )
        return dict(await cur.fetchall())


async def add_discovery_suggestion(guild_id: int, tournament_id: int, players: list[str]) -> bool:
    """Record that a tournament is being suggested to a server. Returns False
    if it already was — this row is what stops a suggestion being posted twice,
    whatever happened to the first one."""
    async with aiosqlite.connect(DB_PATH) as conn:
        try:
            await conn.execute(
                "INSERT INTO discovery_suggestions (guild_id, tournament_id, players) VALUES (?, ?, ?)",
                (guild_id, tournament_id, json.dumps(players)),
            )
            await conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def set_discovery_suggestion_message(guild_id: int, tournament_id: int, message_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE discovery_suggestions SET message_id = ? WHERE guild_id = ? AND tournament_id = ?",
            (message_id, guild_id, tournament_id),
        )
        await conn.commit()


async def set_discovery_suggestion_status(guild_id: int, tournament_id: int, status: str) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE discovery_suggestions SET status = ? WHERE guild_id = ? AND tournament_id = ?",
            (status, guild_id, tournament_id),
        )
        await conn.commit()


async def delete_discovery_suggestion(guild_id: int, tournament_id: int) -> None:
    """Forget a suggestion that never got posted, so a later scan can retry it."""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "DELETE FROM discovery_suggestions WHERE guild_id = ? AND tournament_id = ?",
            (guild_id, tournament_id),
        )
        await conn.commit()


async def get_discovery_suggestion(guild_id: int, tournament_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT status, message_id, players FROM discovery_suggestions "
            "WHERE guild_id = ? AND tournament_id = ?",
            (guild_id, tournament_id),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    return {"status": row[0], "message_id": row[1], "players": json.loads(row[2])}


async def list_suggested_tournament_ids(guild_id: int) -> set[int]:
    """Every tournament already suggested to a server, in any status."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT tournament_id FROM discovery_suggestions WHERE guild_id = ?", (guild_id,)
        )
        return {r[0] for r in await cur.fetchall()}
