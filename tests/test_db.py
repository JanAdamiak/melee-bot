import pytest

import domain.db as db

G = 1  # default guild for single-server tests


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))


async def test_init_creates_players_table():
    import aiosqlite
    await db.init_db()
    async with aiosqlite.connect(db.DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='players'"
        )
        assert await cur.fetchone() is not None


async def test_init_is_idempotent():
    await db.init_db()
    await db.init_db()  # should not raise


async def test_add_player_returns_true():
    await db.init_db()
    assert await db.add_player(G, "Alice", "user#1") is True


async def test_add_duplicate_returns_false():
    await db.init_db()
    await db.add_player(G, "Alice", "user#1")
    assert await db.add_player(G, "Alice", "user#2") is False


async def test_list_players_empty():
    await db.init_db()
    assert await db.list_players(G) == []


async def test_list_players_returns_added():
    await db.init_db()
    await db.add_player(G, "Alice", "u")
    assert "Alice" in await db.list_players(G)


async def test_list_players_sorted_alphabetically():
    await db.init_db()
    for name in ["Zara", "Alice", "Bob"]:
        await db.add_player(G, name, "u")
    assert await db.list_players(G) == ["Alice", "Bob", "Zara"]


async def test_remove_existing_player():
    await db.init_db()
    await db.add_player(G, "Alice", "u")
    assert await db.remove_player(G, "Alice") is True
    assert await db.list_players(G) == []


async def test_remove_nonexistent_player():
    await db.init_db()
    assert await db.remove_player(G, "Nobody") is False


async def test_remove_only_removes_target():
    await db.init_db()
    await db.add_player(G, "Alice", "u")
    await db.add_player(G, "Bob", "u")
    await db.remove_player(G, "Alice")
    assert await db.list_players(G) == ["Bob"]


# ── displayed_name ─────────────────────────────────────────────────────────────

async def test_add_player_with_display_name():
    await db.init_db()
    assert await db.add_player(G, "Alice", "u", "Ali") is True


async def test_get_display_names_empty():
    await db.init_db()
    await db.add_player(G, "Alice", "u")
    assert await db.get_display_names(G) == {}


async def test_get_display_names_returns_set_names():
    await db.init_db()
    await db.add_player(G, "Alice", "u", "Ali")
    await db.add_player(G, "Bob", "u")
    assert await db.get_display_names(G) == {"Alice": "Ali"}


async def test_set_player_display_name_returns_true():
    await db.init_db()
    await db.add_player(G, "Alice", "u")
    assert await db.set_player_display_name(G, "Alice", "Ali") is True
    assert await db.get_display_names(G) == {"Alice": "Ali"}


async def test_set_player_display_name_unknown_returns_false():
    await db.init_db()
    assert await db.set_player_display_name(G, "Nobody", "X") is False


async def test_set_player_display_name_clears_with_none():
    await db.init_db()
    await db.add_player(G, "Alice", "u", "Ali")
    await db.set_player_display_name(G, "Alice", None)
    assert await db.get_display_names(G) == {}


async def test_display_name_survives_reinit():
    """init_db called twice should not wipe the displayed_name column."""
    await db.init_db()
    await db.add_player(G, "Alice", "u", "Ali")
    await db.init_db()  # second call (migration path)
    assert await db.get_display_names(G) == {"Alice": "Ali"}


# ── tournament bindings ────────────────────────────────────────────────────────

async def test_add_tournament_returns_true():
    await db.init_db()
    assert await db.add_tournament(G, 1001, "user#1") is True


async def test_add_duplicate_tournament_returns_false():
    await db.init_db()
    await db.add_tournament(G, 1001, "user#1")
    assert await db.add_tournament(G, 1001, "user#2") is False


async def test_remove_tournament_returns_true():
    await db.init_db()
    await db.add_tournament(G, 1001, "u")
    assert await db.remove_tournament(G, 1001) is True


async def test_remove_nonexistent_tournament_returns_false():
    await db.init_db()
    assert await db.remove_tournament(G, 9999) is False


async def test_remove_tournament_also_removes_players():
    await db.init_db()
    await db.add_tournament(G, 1001, "u")
    await db.set_tournament_players(G, 1001, ["Alice", "Bob"])
    await db.remove_tournament(G, 1001)
    assert await db.get_tournament_players(G, 1001) == []


async def test_set_tournament_concluded():
    await db.init_db()
    await db.add_tournament(G, 1001, "u")
    await db.set_tournament_concluded(G, 1001)
    tournaments = await db.list_tournaments(G)
    assert tournaments[0]["concluded"] is True


async def test_set_tournament_players_replaces_existing():
    await db.init_db()
    await db.add_tournament(G, 1001, "u")
    await db.set_tournament_players(G, 1001, ["Alice", "Bob"])
    await db.set_tournament_players(G, 1001, ["Charlie"])
    assert await db.get_tournament_players(G, 1001) == ["Charlie"]


async def test_get_tournament_players_sorted():
    await db.init_db()
    await db.add_tournament(G, 1001, "u")
    await db.set_tournament_players(G, 1001, ["Zara", "Alice", "Bob"])
    assert await db.get_tournament_players(G, 1001) == ["Alice", "Bob", "Zara"]


async def test_get_tournament_players_unknown_tournament():
    await db.init_db()
    assert await db.get_tournament_players(G, 9999) == []


async def test_list_tournaments_empty():
    await db.init_db()
    assert await db.list_tournaments(G) == []


async def test_list_tournaments_includes_players_and_status():
    await db.init_db()
    await db.add_tournament(G, 1001, "u")
    await db.set_tournament_players(G, 1001, ["Alice"])
    result = await db.list_tournaments(G)
    assert len(result) == 1
    assert result[0]["id"] == 1001
    assert result[0]["concluded"] is False
    assert result[0]["players"] == ["Alice"]


async def test_list_tournaments_multiple_sorted_by_id():
    await db.init_db()
    await db.add_tournament(G, 2000, "u")
    await db.add_tournament(G, 1000, "u")
    ids = [t["id"] for t in await db.list_tournaments(G)]
    assert ids == [1000, 2000]


# ── shared tournament facts ─────────────────────────────────────────────────────

async def test_tournament_info_defaults_to_none():
    await db.init_db()
    await db.add_tournament(G, 1001, "u", name="Open")
    assert await db.get_tournament_info(1001) == {"name": "Open", "start_time": None, "status": None}


async def test_tournament_info_unknown_tournament():
    await db.init_db()
    assert await db.get_tournament_info(9999) is None


async def test_set_tournament_info_round_trips_utc_start_time():
    import datetime
    await db.init_db()
    await db.add_tournament(G, 1001, "u")
    start = datetime.datetime(2026, 7, 24, 16, 0, tzinfo=datetime.UTC)
    await db.set_tournament_info(1001, start, "In Progress")
    info = await db.get_tournament_info(1001)
    assert info["start_time"] == start
    assert info["status"] == "In Progress"
    listed = (await db.list_tournaments(G))[0]
    assert listed["start_time"] == start and listed["status"] == "In Progress"


async def test_set_tournament_info_none_keeps_stored_value():
    import datetime
    await db.init_db()
    await db.add_tournament(G, 1001, "u")
    start = datetime.datetime(2026, 7, 24, 16, 0, tzinfo=datetime.UTC)
    await db.set_tournament_info(1001, start, "In Progress")
    await db.set_tournament_info(1001, None, "Ended")
    assert await db.get_tournament_info(1001) == {"name": None, "start_time": start, "status": "Ended"}


async def test_add_tournament_fills_a_missing_name_but_keeps_an_existing_one():
    await db.init_db()
    await db.add_tournament(G, 1001, "u")
    await db.add_tournament(G + 1, 1001, "v", name="Open")
    assert await db.get_tournament_name(1001) == "Open"
    await db.add_tournament(G + 2, 1001, "w", name="Something Else")
    assert await db.get_tournament_name(1001) == "Open"


# ── rounds_to_track ─────────────────────────────────────────────────────────────

async def test_add_tournament_with_rounds_to_track():
    await db.init_db()
    await db.add_tournament(G, 1001, "u", rounds_to_track=9)
    assert await db.get_tournament_rounds_to_track(G, 1001) == 9


async def test_get_tournament_rounds_to_track_defaults_to_none():
    await db.init_db()
    await db.add_tournament(G, 1001, "u")
    assert await db.get_tournament_rounds_to_track(G, 1001) is None


async def test_get_tournament_rounds_to_track_unknown_tournament():
    await db.init_db()
    assert await db.get_tournament_rounds_to_track(G, 9999) is None


async def test_set_tournament_rounds_to_track_updates_existing():
    await db.init_db()
    await db.add_tournament(G, 1001, "u", rounds_to_track=9)
    await db.set_tournament_rounds_to_track(G, 1001, 11)
    assert await db.get_tournament_rounds_to_track(G, 1001) == 11


async def test_list_tournaments_includes_rounds_to_track():
    await db.init_db()
    await db.add_tournament(G, 1001, "u", rounds_to_track=9)
    result = await db.list_tournaments(G)
    assert result[0]["rounds_to_track"] == 9


# ── watch sessions ─────────────────────────────────────────────────────────────

async def test_save_and_load_watch_session():
    await db.init_db()
    await db.save_watch_session(guild_id=G, channel_id=111, message_id=999, tournament_ids=[1001, 1002])
    sessions = await db.load_watch_sessions()
    assert len(sessions) == 1
    guild_id, channel_id, message_id, tournament_ids = sessions[0]
    assert guild_id == G
    assert channel_id == 111
    assert message_id == 999
    assert tournament_ids == [1001, 1002]


async def test_save_watch_session_upserts():
    await db.init_db()
    await db.save_watch_session(G, 111, 999, [1001])
    await db.save_watch_session(G, 111, 888, [1002])  # same channel, new message
    sessions = await db.load_watch_sessions()
    assert len(sessions) == 1
    assert sessions[0][2] == 888
    assert sessions[0][3] == [1002]


async def test_update_watch_session_message():
    await db.init_db()
    await db.save_watch_session(G, 111, 999, [1001])
    await db.update_watch_session_message(111, 777)
    sessions = await db.load_watch_sessions()
    assert sessions[0][2] == 777


async def test_delete_watch_session():
    await db.init_db()
    await db.save_watch_session(G, 111, 999, [1001])
    await db.delete_watch_session(111)
    assert await db.load_watch_sessions() == []


async def test_load_sessions_empty():
    await db.init_db()
    assert await db.load_watch_sessions() == []


# ── all new tables created by init_db ─────────────────────────────────────────

async def test_init_creates_all_tables():
    import aiosqlite
    await db.init_db()
    async with aiosqlite.connect(db.DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = {r[0] for r in await cur.fetchall()}
    assert {
        "players", "tournaments", "guild_tournaments", "tournament_players",
        "watch_sessions", "round_results", "topcut_sessions", "round_match_cache",
    } <= tables


# ── round result cache ─────────────────────────────────────────────────────────

async def test_load_round_results_empty():
    await db.init_db()
    assert await db.load_round_results(9999) == {}


async def test_save_and_load_round_results():
    await db.init_db()
    data = {"Alice": {"Round 1": ("2-0", "Bob"), "Round 2": ("0-2", "Charlie")}}
    await db.save_round_results(1001, data)
    loaded = await db.load_round_results(1001)
    assert loaded == data


async def test_save_round_results_upserts():
    await db.init_db()
    await db.save_round_results(1001, {"Alice": {"Round 1": ("Not reported", "Bob")}})
    await db.save_round_results(1001, {"Alice": {"Round 1": ("2-0", "Bob")}})
    loaded = await db.load_round_results(1001)
    assert loaded["Alice"]["Round 1"] == ("2-0", "Bob")


async def test_save_round_results_multiple_players():
    await db.init_db()
    data = {
        "Alice": {"Round 1": ("2-0", "Bob")},
        "Bob":   {"Round 1": ("0-2", "Alice")},
    }
    await db.save_round_results(1001, data)
    loaded = await db.load_round_results(1001)
    assert loaded["Alice"]["Round 1"] == ("2-0", "Bob")
    assert loaded["Bob"]["Round 1"] == ("0-2", "Alice")


async def test_save_round_results_isolated_by_tournament():
    await db.init_db()
    await db.save_round_results(1001, {"Alice": {"Round 1": ("2-0", "Bob")}})
    assert await db.load_round_results(2002) == {}


# ── topcut session persistence ─────────────────────────────────────────────────

async def test_save_and_load_topcut_session():
    await db.init_db()
    await db.save_topcut_session(G, 555, 999, 430189, 8, "user#1")
    sessions = await db.load_topcut_sessions()
    assert sessions == [(G, 555, 999, 430189, 8, "user#1")]


async def test_save_topcut_session_upserts_on_conflict():
    await db.init_db()
    await db.save_topcut_session(G, 555, 999, 430189, 8, "user#1")
    await db.save_topcut_session(G, 555, 1000, 430190, 16, "user#1")
    sessions = await db.load_topcut_sessions()
    assert sessions == [(G, 555, 1000, 430190, 16, "user#1")]


async def test_update_topcut_session_message():
    await db.init_db()
    await db.save_topcut_session(G, 555, 999, 430189, 8, "user#1")
    await db.update_topcut_session_message(555, 1234)
    sessions = await db.load_topcut_sessions()
    assert sessions[0][2] == 1234


async def test_delete_topcut_session():
    await db.init_db()
    await db.save_topcut_session(G, 555, 999, 430189, 8, "user#1")
    await db.delete_topcut_session(555)
    assert await db.load_topcut_sessions() == []


async def test_load_topcut_sessions_empty():
    await db.init_db()
    assert await db.load_topcut_sessions() == []


# ── round match cache ───────────────────────────────────────────────────────────

async def test_save_and_load_round_match_cache():
    await db.init_db()
    matches = [{"TableNumber": 1, "ResultString": "2-0"}]
    await db.save_round_match_cache(430189, 1001, matches)
    cache = await db.load_round_match_cache(430189)
    assert cache == {1001: matches}


async def test_round_match_cache_upserts():
    await db.init_db()
    await db.save_round_match_cache(430189, 1001, [{"a": 1}])
    await db.save_round_match_cache(430189, 1001, [{"a": 2}])
    cache = await db.load_round_match_cache(430189)
    assert cache == {1001: [{"a": 2}]}


async def test_round_match_cache_multiple_rounds():
    await db.init_db()
    await db.save_round_match_cache(430189, 1001, [{"a": 1}])
    await db.save_round_match_cache(430189, 1002, [{"a": 2}])
    cache = await db.load_round_match_cache(430189)
    assert set(cache) == {1001, 1002}


async def test_round_match_cache_isolated_by_tournament():
    await db.init_db()
    await db.save_round_match_cache(430189, 1001, [{"a": 1}])
    assert await db.load_round_match_cache(999999) == {}


# ── discovery settings ─────────────────────────────────────────────────────────

async def test_discovery_is_off_by_default():
    await db.init_db()
    assert await db.get_discovery_channel(G) is None
    assert await db.list_discovery_channels() == {}


async def test_set_discovery_channel_is_per_guild():
    await db.init_db()
    await db.set_discovery_channel(G, 555)
    await db.set_discovery_channel(G + 1, 666)
    assert await db.get_discovery_channel(G) == 555
    assert await db.list_discovery_channels() == {G: 555, G + 1: 666}


async def test_set_discovery_channel_replaces_and_turns_off():
    await db.init_db()
    await db.set_discovery_channel(G, 555)
    await db.set_discovery_channel(G, 777)
    assert await db.get_discovery_channel(G) == 777
    await db.set_discovery_channel(G, None)
    assert await db.get_discovery_channel(G) is None
    assert await db.list_discovery_channels() == {}


# ── discovery suggestions ──────────────────────────────────────────────────────

async def test_add_discovery_suggestion_starts_pending():
    await db.init_db()
    assert await db.add_discovery_suggestion(G, 1001, ["Alice", "Bob"]) is True
    assert await db.get_discovery_suggestion(G, 1001) == {
        "status": db.SUGGESTION_PENDING, "message_id": None, "players": ["Alice", "Bob"],
    }


async def test_same_tournament_cannot_be_suggested_twice_to_a_guild():
    await db.init_db()
    assert await db.add_discovery_suggestion(G, 1001, ["Alice"]) is True
    assert await db.add_discovery_suggestion(G, 1001, ["Alice", "Bob"]) is False
    assert (await db.get_discovery_suggestion(G, 1001))["players"] == ["Alice"]


async def test_ignored_and_bound_suggestions_still_block_a_repeat():
    await db.init_db()
    await db.add_discovery_suggestion(G, 1001, ["Alice"])
    await db.add_discovery_suggestion(G, 1002, ["Alice"])
    await db.set_discovery_suggestion_status(G, 1001, db.SUGGESTION_IGNORED)
    await db.set_discovery_suggestion_status(G, 1002, db.SUGGESTION_BOUND)
    assert await db.add_discovery_suggestion(G, 1001, ["Alice"]) is False
    assert await db.list_suggested_tournament_ids(G) == {1001, 1002}
    assert (await db.get_discovery_suggestion(G, 1001))["status"] == db.SUGGESTION_IGNORED


async def test_suggestions_are_per_guild():
    await db.init_db()
    await db.add_discovery_suggestion(G, 1001, ["Alice"])
    assert await db.add_discovery_suggestion(G + 1, 1001, ["Bob"]) is True
    assert await db.list_suggested_tournament_ids(G + 1) == {1001}
    assert await db.get_discovery_suggestion(G + 2, 1001) is None


async def test_set_discovery_suggestion_message():
    await db.init_db()
    await db.add_discovery_suggestion(G, 1001, ["Alice"])
    await db.set_discovery_suggestion_message(G, 1001, 424242)
    assert (await db.get_discovery_suggestion(G, 1001))["message_id"] == 424242


async def test_deleted_suggestion_can_be_suggested_again():
    await db.init_db()
    await db.add_discovery_suggestion(G, 1001, ["Alice"])
    await db.delete_discovery_suggestion(G, 1001)
    assert await db.list_suggested_tournament_ids(G) == set()
    assert await db.add_discovery_suggestion(G, 1001, ["Alice"]) is True
