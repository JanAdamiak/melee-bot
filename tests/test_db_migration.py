"""Schema versioning and the single-server → multi-server migration."""

import os
import sqlite3
from unittest.mock import patch

import aiosqlite
import pytest

import domain.db as db

GUILD = 111
OTHER_GUILD = 222

# The schema as it stood before PRAGMA user_version was introduced (version 0).
_V0_SCHEMA = """
CREATE TABLE players (
    name           TEXT PRIMARY KEY,
    added_by       TEXT NOT NULL,
    added_at       TEXT NOT NULL DEFAULT (datetime('now')),
    displayed_name TEXT
);
CREATE TABLE tournaments (
    tournament_id   INTEGER PRIMARY KEY,
    concluded       INTEGER NOT NULL DEFAULT 0,
    added_by        TEXT NOT NULL,
    added_at        TEXT NOT NULL DEFAULT (datetime('now')),
    rounds_to_track INTEGER,
    name            TEXT
);
CREATE TABLE tournament_players (
    tournament_id INTEGER NOT NULL,
    player_name   TEXT NOT NULL,
    PRIMARY KEY (tournament_id, player_name)
);
CREATE TABLE watch_sessions (
    channel_id     INTEGER PRIMARY KEY,
    message_id     INTEGER NOT NULL,
    tournament_ids TEXT NOT NULL
);
CREATE TABLE round_results (
    tournament_id INTEGER NOT NULL,
    player        TEXT NOT NULL,
    round_name    TEXT NOT NULL,
    result        TEXT NOT NULL,
    opponent      TEXT NOT NULL,
    PRIMARY KEY (tournament_id, player, round_name)
);
CREATE TABLE topcut_sessions (
    channel_id    INTEGER PRIMARY KEY,
    message_id    INTEGER NOT NULL,
    tournament_id INTEGER NOT NULL,
    cut_size      INTEGER NOT NULL,
    started_by    TEXT NOT NULL,
    started_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE round_match_cache (
    tournament_id INTEGER NOT NULL,
    round_id      INTEGER NOT NULL,
    matches_json  TEXT NOT NULL,
    PRIMARY KEY (tournament_id, round_id)
);
INSERT INTO players (name, added_by, displayed_name) VALUES ('Alice', 'u#1', 'Ali'), ('Bob', 'u#1', NULL);
INSERT INTO tournaments (tournament_id, concluded, added_by, rounds_to_track, name)
    VALUES (1001, 1, 'u#1', 9, 'Old Open'), (1002, 0, 'u#2', NULL, NULL);
INSERT INTO tournament_players VALUES (1001, 'Alice'), (1001, 'Bob'), (1002, 'Bob');
INSERT INTO watch_sessions VALUES (555, 999, '[1002]');
INSERT INTO round_results VALUES (1001, 'Alice', 'Round 1', '2-0', 'Zed');
INSERT INTO topcut_sessions (channel_id, message_id, tournament_id, cut_size, started_by)
    VALUES (556, 998, 1002, 8, 'u#1');
"""


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))


def _make_v0_db(script: str = _V0_SCHEMA) -> None:
    conn = sqlite3.connect(db.DB_PATH)
    conn.executescript(script)
    conn.commit()
    conn.close()


async def _user_version() -> int:
    async with aiosqlite.connect(db.DB_PATH) as conn:
        cur = await conn.execute("PRAGMA user_version")
        return (await cur.fetchone())[0]


# ── versioning ─────────────────────────────────────────────────────────────────

async def test_fresh_db_is_at_latest_version():
    await db.init_db()
    assert await _user_version() == db.SCHEMA_VERSION


async def test_fresh_db_needs_no_guild_and_makes_no_backup():
    await db.init_db()
    assert not os.path.exists(f"{db.DB_PATH}.v0.bak")


async def test_init_is_noop_once_current():
    await db.init_db()
    await db.add_player(GUILD, "Alice", "u")
    await db.init_db()
    assert await db.list_players(GUILD) == ["Alice"]
    assert not os.path.exists(f"{db.DB_PATH}.v0.bak")


# ── v0 → current ───────────────────────────────────────────────────────────────

async def test_migration_assigns_existing_rows_to_the_legacy_guild():
    _make_v0_db()
    await db.init_db(legacy_guild_id=GUILD)

    assert await _user_version() == db.SCHEMA_VERSION
    assert await db.list_players(GUILD) == ["Alice", "Bob"]
    assert await db.get_display_names(GUILD) == {"Alice": "Ali"}

    tournaments = {t["id"]: t for t in await db.list_tournaments(GUILD)}
    assert tournaments[1001] == {
        "id": 1001, "concluded": True, "players": ["Alice", "Bob"],
        "rounds_to_track": 9, "name": "Old Open", "start_time": None, "status": None,
    }
    assert tournaments[1002]["concluded"] is False
    assert tournaments[1002]["players"] == ["Bob"]

    assert await db.load_watch_sessions() == [(GUILD, 555, 999, [1002])]
    assert await db.load_topcut_sessions() == [(GUILD, 556, 998, 1002, 8, "u#1")]
    assert await db.load_round_results(1001) == {"Alice": {"Round 1": ("2-0", "Zed")}}


async def test_migration_leaves_other_guilds_empty():
    _make_v0_db()
    await db.init_db(legacy_guild_id=GUILD)
    assert await db.list_players(OTHER_GUILD) == []
    assert await db.list_tournaments(OTHER_GUILD) == []


async def test_migration_backs_up_the_old_file_first():
    _make_v0_db()
    await db.init_db(legacy_guild_id=GUILD)

    backup = sqlite3.connect(f"{db.DB_PATH}.v0.bak")
    try:
        cols = [r[1] for r in backup.execute("PRAGMA table_info(players)")]
        assert "guild_id" not in cols  # still the old shape
        assert backup.execute("SELECT COUNT(*) FROM players").fetchone()[0] == 2
    finally:
        backup.close()


async def test_migration_drops_the_renamed_old_tables():
    _make_v0_db()
    await db.init_db(legacy_guild_id=GUILD)
    async with aiosqlite.connect(db.DB_PATH) as conn:
        cur = await conn.execute("SELECT name FROM sqlite_master WHERE name LIKE '%_v0'")
        assert await cur.fetchall() == []


async def test_migration_without_guild_id_fails_and_changes_nothing():
    _make_v0_db()
    with pytest.raises(RuntimeError, match="GUILD_ID"):
        await db.init_db()
    assert await _user_version() == 0
    conn = sqlite3.connect(db.DB_PATH)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(players)")]
        assert "guild_id" not in cols
        assert conn.execute("SELECT COUNT(*) FROM players").fetchone()[0] == 2
    finally:
        conn.close()


async def test_migration_handles_db_older_than_the_optional_columns():
    """Very old files never got displayed_name / rounds_to_track / name."""
    _make_v0_db("""
        CREATE TABLE players (name TEXT PRIMARY KEY, added_by TEXT NOT NULL,
                              added_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE tournaments (tournament_id INTEGER PRIMARY KEY, concluded INTEGER NOT NULL DEFAULT 0,
                                  added_by TEXT NOT NULL, added_at TEXT NOT NULL DEFAULT (datetime('now')));
        INSERT INTO players (name, added_by) VALUES ('Alice', 'u');
        INSERT INTO tournaments (tournament_id, added_by) VALUES (1001, 'u');
    """)
    await db.init_db(legacy_guild_id=GUILD)
    assert await db.list_players(GUILD) == ["Alice"]
    assert [t["id"] for t in await db.list_tournaments(GUILD)] == [1001]
    await db.save_watch_session(GUILD, 1, 2, [1001])  # tables absent from the old file exist now
    assert await db.load_watch_sessions() == [(GUILD, 1, 2, [1001])]


# ── two servers stay separate ──────────────────────────────────────────────────

async def test_same_username_on_two_rosters_with_different_display_names():
    await db.init_db()
    assert await db.add_player(GUILD, "Alice", "u", "Ali") is True
    assert await db.add_player(OTHER_GUILD, "Alice", "v", "Big Al") is True
    assert await db.get_display_names(GUILD) == {"Alice": "Ali"}
    assert await db.get_display_names(OTHER_GUILD) == {"Alice": "Big Al"}


async def test_removing_a_player_only_affects_that_guild():
    await db.init_db()
    await db.add_player(GUILD, "Alice", "u")
    await db.add_player(OTHER_GUILD, "Alice", "v")
    assert await db.remove_player(GUILD, "Alice") is True
    assert await db.list_players(GUILD) == []
    assert await db.list_players(OTHER_GUILD) == ["Alice"]


async def test_both_guilds_can_bind_the_same_tournament_with_their_own_players():
    await db.init_db()
    assert await db.add_tournament(GUILD, 1001, "u", rounds_to_track=9, name="Open") is True
    assert await db.add_tournament(OTHER_GUILD, 1001, "v") is True
    await db.set_tournament_players(GUILD, 1001, ["Alice"])
    await db.set_tournament_players(OTHER_GUILD, 1001, ["Bob", "Cara"])

    assert await db.get_tournament_players(GUILD, 1001) == ["Alice"]
    assert await db.get_tournament_players(OTHER_GUILD, 1001) == ["Bob", "Cara"]
    assert await db.get_tournament_rounds_to_track(GUILD, 1001) == 9
    assert await db.get_tournament_rounds_to_track(OTHER_GUILD, 1001) is None
    # the event's own facts are shared
    assert (await db.list_tournaments(OTHER_GUILD))[0]["name"] == "Open"


async def test_concluding_is_per_guild():
    await db.init_db()
    await db.add_tournament(GUILD, 1001, "u")
    await db.add_tournament(OTHER_GUILD, 1001, "v")
    await db.set_tournament_concluded(GUILD, 1001)
    assert (await db.list_tournaments(GUILD))[0]["concluded"] is True
    assert (await db.list_tournaments(OTHER_GUILD))[0]["concluded"] is False


async def test_unbinding_in_one_guild_keeps_the_other_guilds_binding():
    await db.init_db()
    await db.add_tournament(GUILD, 1001, "u", name="Open")
    await db.add_tournament(OTHER_GUILD, 1001, "v")
    await db.set_tournament_players(OTHER_GUILD, 1001, ["Bob"])

    assert await db.remove_tournament(GUILD, 1001) is True
    assert await db.list_tournaments(GUILD) == []
    assert await db.get_tournament_players(OTHER_GUILD, 1001) == ["Bob"]
    assert await db.get_tournament_name(1001) == "Open"

    await db.remove_tournament(OTHER_GUILD, 1001)
    assert await db.get_tournament_name(1001) is None  # shared row goes with the last binding


# ── v1 → v2 ────────────────────────────────────────────────────────────────────

async def test_v1_database_gains_last_change_and_keeps_its_data():
    # Build a v1 file by running only the first migration.
    with patch.object(db, "_MIGRATIONS", db._MIGRATIONS[:1]), patch.object(db, "SCHEMA_VERSION", 1):
        await db.init_db()
        await db.add_tournament(GUILD, 1001, "u", name="Open")
    assert await _user_version() == 1

    await db.init_db()
    assert await _user_version() == db.SCHEMA_VERSION
    assert await db.get_tournament_name(1001) == "Open"
    assert await db.get_tournament_last_change(1001) is None
    assert os.path.exists(f"{db.DB_PATH}.v1.bak")


# ── v2 → v3 ────────────────────────────────────────────────────────────────────

async def test_v2_database_gains_the_discovery_tables_and_keeps_its_data():
    with patch.object(db, "_MIGRATIONS", db._MIGRATIONS[:2]), patch.object(db, "SCHEMA_VERSION", 2):
        await db.init_db()
        await db.add_player(GUILD, "Alice", "u")
    assert await _user_version() == 2

    await db.init_db()
    assert await _user_version() == db.SCHEMA_VERSION
    assert await db.list_players(GUILD) == ["Alice"]
    assert await db.get_discovery_channel(GUILD) is None
    assert await db.add_discovery_suggestion(GUILD, 1001, ["Alice"]) is True
    assert os.path.exists(f"{db.DB_PATH}.v2.bak")
