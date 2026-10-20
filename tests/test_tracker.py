from unittest.mock import AsyncMock, patch

import pytest

from domain.tracker import (
    PlayerSnapshot,
    TournamentSnapshot,
    _comp_player_names,
    _find_match,
    _find_standing,
    _is_final_round,
    _player_names,
    diff,
    fetch_snapshot,
    parse_tournament_id,
)

# Patch targets: tracker.py re-exports these from melee_tiebreakers.melee_client,
# but the underlying HTTP calls are made from melee_client's own module globals,
# so mocks must target that module, not tracker.
_MC_GET = "melee_tiebreakers.melee_client._get"
_MC_POST = "melee_tiebreakers.melee_client._post"

# ── parse_tournament_id ────────────────────────────────────────────────────────

def test_parse_numeric_id():
    assert parse_tournament_id("430189") == 430189

def test_parse_full_url():
    assert parse_tournament_id("https://melee.gg/Tournament/View/430189") == 430189

def test_parse_url_trailing_slash():
    assert parse_tournament_id("https://melee.gg/Tournament/View/430189/") == 430189

def test_parse_with_leading_whitespace():
    assert parse_tournament_id("  430189  ") == 430189

def test_parse_invalid_raises():
    with pytest.raises(ValueError):
        parse_tournament_id("not-an-id")

def test_parse_empty_raises():
    with pytest.raises(ValueError):
        parse_tournament_id("")


# ── _player_names ──────────────────────────────────────────────────────────────

def _match(p1: str, p2: str | None = "Bob") -> dict:
    competitors = [{"Team": {"Players": [{"Username": p1}]}}]
    if p2:
        competitors.append({"Team": {"Players": [{"Username": p2}]}})
    return {"Competitors": competitors}


def test_player_names_normal():
    assert _player_names(_match("Alice", "Bob")) == ("Alice", "Bob")

def test_player_names_single_competitor_is_bye():
    assert _player_names(_match("Alice", None)) == ("Alice", "BYE")

def test_player_names_empty_competitors():
    # no competitors at all — first slot is unknown, second is BYE (same as single-competitor case)
    assert _player_names({"Competitors": []}) == ("?", "BYE")

def test_player_names_display_name_fallback():
    m = {
        "Competitors": [
            {"Team": {"Players": [{"Username": None, "DisplayName": "Alice D"}]}},
            {"Team": {"Players": [{"Username": "Bob"}]}},
        ]
    }
    assert _player_names(m) == ("Alice D", "Bob")

def test_player_names_no_players_key():
    m = {"Competitors": [{"Team": {}}, {"Team": {"Players": [{"Username": "Bob"}]}}]}
    assert _player_names(m) == ("?", "Bob")


# ── _find_match ────────────────────────────────────────────────────────────────

def test_find_match_exact():
    matches = [_match("alice", "bob")]
    assert _find_match(matches, "alice") is not None

def test_find_match_case_insensitive():
    matches = [_match("AliceG_XYZ", "Bob")]
    assert _find_match(matches, "aliceg_xyz") is not None

def test_find_match_as_opponent():
    matches = [_match("Alice", "mossgoblin")]
    assert _find_match(matches, "mossgoblin") is not None

def test_find_match_not_found():
    matches = [_match("Alice", "Bob")]
    assert _find_match(matches, "charlie") is None

def test_find_match_empty_list():
    assert _find_match([], "alice") is None

def test_find_match_team_second_player():
    # Player is players[1] on a team — must still be found
    m = {"Competitors": [
        {"Team": {"Players": [{"Username": "partner"}, {"Username": "alice"}]}},
        {"Team": {"Players": [{"Username": "opponent"}]}},
    ]}
    assert _find_match([m], "alice") is not None


# ── _comp_player_names ─────────────────────────────────────────────────────────

def test_comp_player_names_username_only():
    comp = {"Team": {"Players": [{"Username": "MistyComet"}]}}
    assert _comp_player_names(comp) == ["MistyComet"]

def test_comp_player_names_includes_display_name_when_different():
    comp = {"Team": {"Players": [{"Username": "MistyComet", "DisplayName": "Yael Millbrook"}]}}
    names = _comp_player_names(comp)
    assert "MistyComet" in names
    assert "Yael Millbrook" in names

def test_comp_player_names_no_duplicate_when_same():
    comp = {"Team": {"Players": [{"Username": "Alice", "DisplayName": "Alice"}]}}
    assert _comp_player_names(comp) == ["Alice"]

def test_comp_player_names_display_name_fallback():
    comp = {"Team": {"Players": [{"Username": None, "DisplayName": "Alice D"}]}}
    assert _comp_player_names(comp) == ["Alice D"]

def test_comp_player_names_multi_player_team():
    comp = {"Team": {"Players": [{"Username": "EmberOsprey"}, {"Username": "Yael Millbrook"}]}}
    assert _comp_player_names(comp) == ["EmberOsprey", "Yael Millbrook"]


# ── _is_final_round ────────────────────────────────────────────────────────────

def test_is_final_round_finals():
    assert _is_final_round({"name": "Finals"}) is True

def test_is_final_round_grand_finals():
    assert _is_final_round({"name": "Grand Finals"}) is True

def test_is_final_round_case_insensitive():
    assert _is_final_round({"name": "FINALS"}) is True

def test_is_final_round_semifinals_not_final():
    assert _is_final_round({"name": "Semifinals"}) is False

def test_is_final_round_swiss_not_final():
    assert _is_final_round({"name": "Round 6"}) is False

def test_is_final_round_top8_not_final():
    assert _is_final_round({"name": "Top 8"}) is False


# ── _find_standing ─────────────────────────────────────────────────────────────

def _standing(username: str, rank: int = 1) -> dict:
    return {"Rank": rank, "Team": {"Players": [{"Username": username}]}}


def test_find_standing_found():
    standings = [_standing("alice", 1), _standing("bob", 2)]
    assert _find_standing(standings, "alice") is not None

def test_find_standing_case_insensitive():
    standings = [_standing("AliceG_XYZ")]
    assert _find_standing(standings, "aliceg_xyz") is not None

def test_find_standing_not_found():
    standings = [_standing("alice")]
    assert _find_standing(standings, "charlie") is None


# ── TournamentSnapshot.is_complete ─────────────────────────────────────────────

def test_is_complete_all_results_in():
    snap = TournamentSnapshot(tournament_id=1, last_round_is_final=True, players={
        "Alice": PlayerSnapshot(rounds={"Finals": "2-0"}),
        "Bob":   PlayerSnapshot(rounds={"Finals": "0-2"}),
    })
    assert snap.is_complete() is True

def test_is_complete_not_final_round():
    # All results reported but we're only in swiss — must not auto-conclude
    snap = TournamentSnapshot(tournament_id=1, last_round_is_final=False, players={
        "Alice": PlayerSnapshot(rounds={"Round 6": "2-0"}),
    })
    assert snap.is_complete() is False

def test_is_complete_one_pending():
    snap = TournamentSnapshot(tournament_id=1, last_round_is_final=True, players={
        "Alice": PlayerSnapshot(rounds={"Finals": "2-0", "Semifinals": "Not reported"}),
    })
    assert snap.is_complete() is False

def test_is_complete_no_rounds():
    snap = TournamentSnapshot(tournament_id=1, last_round_is_final=True, players={"Alice": PlayerSnapshot()})
    assert snap.is_complete() is False

def test_is_complete_no_players():
    assert TournamentSnapshot(tournament_id=1).is_complete() is False


# ── TournamentSnapshot.is_complete (rounds_to_track) ───────────────────────────

def test_is_complete_rounds_to_track_reached():
    # last_round_is_final is False (e.g. unnamed top-cut round) but the
    # requested round count has been reached and all results are in.
    snap = TournamentSnapshot(tournament_id=1, last_round_is_final=False, rounds_started=9, players={
        "Alice": PlayerSnapshot(rounds={"Round 9": "2-0"}),
    })
    assert snap.is_complete(rounds_to_track=9) is True

def test_is_complete_rounds_to_track_not_reached():
    snap = TournamentSnapshot(tournament_id=1, last_round_is_final=False, rounds_started=8, players={
        "Alice": PlayerSnapshot(rounds={"Round 8": "2-0"}),
    })
    assert snap.is_complete(rounds_to_track=9) is False

def test_is_complete_rounds_to_track_ignores_last_round_is_final():
    # Round count reached but a result is still pending — must not conclude
    # even though last_round_is_final happens to be True.
    snap = TournamentSnapshot(tournament_id=1, last_round_is_final=True, rounds_started=9, players={
        "Alice": PlayerSnapshot(rounds={"Round 9": "Not reported"}),
    })
    assert snap.is_complete(rounds_to_track=9) is False


# ── diff ───────────────────────────────────────────────────────────────────────

def _snap(tournament_id=1, **players) -> TournamentSnapshot:
    """Convenience: _snap(Alice=PlayerSnapshot(...))"""
    return TournamentSnapshot(tournament_id=tournament_id, players=players)


def test_diff_no_change():
    ps = PlayerSnapshot(rounds={"Round 1": "2-0"}, opponents={"Round 1": "Bob"})
    snap = _snap(Alice=ps)
    assert diff(snap, snap) == []

def test_diff_result_reported_win():
    old = _snap(Alice=PlayerSnapshot(rounds={"Round 1": "Not reported"}, opponents={"Round 1": "Bob"}))
    new = _snap(Alice=PlayerSnapshot(rounds={"Round 1": "2-0"}, opponents={"Round 1": "Bob"}))
    changes = diff(old, new)
    assert len(changes) == 1
    assert "2-0" in changes[0].description
    assert changes[0].positive is True

def test_diff_result_reported_loss():
    old = _snap(Alice=PlayerSnapshot(rounds={"Round 1": "Not reported"}, opponents={"Round 1": "Bob"}))
    new = _snap(Alice=PlayerSnapshot(rounds={"Round 1": "0-2"}, opponents={"Round 1": "Bob"}))
    changes = diff(old, new)
    assert changes[0].positive is False

def test_diff_result_reported_draw():
    old = _snap(Alice=PlayerSnapshot(rounds={"Round 1": "Not reported"}, opponents={"Round 1": "Bob"}))
    new = _snap(Alice=PlayerSnapshot(rounds={"Round 1": "1-1"}, opponents={"Round 1": "Bob"}))
    changes = diff(old, new)
    assert changes[0].positive is False  # draw is not positive

def test_diff_melee_draw_format():
    old = _snap(Alice=PlayerSnapshot(rounds={"Round 1": "Not reported"}, opponents={"Round 1": "Bob"}))
    new = _snap(Alice=PlayerSnapshot(rounds={"Round 1": "1-1-0 Draw"}, opponents={"Round 1": "Bob"}))
    changes = diff(old, new)
    assert changes[0].positive is None

def test_diff_melee_win_format():
    old = _snap(Alice=PlayerSnapshot(rounds={"Round 1": "Not reported"}, opponents={"Round 1": "Bob"}))
    new = _snap(Alice=PlayerSnapshot(rounds={"Round 1": "Alice won 2-0-0"}, opponents={"Round 1": "Bob"}))
    changes = diff(old, new)
    assert changes[0].positive is True

def test_diff_melee_loss_format():
    old = _snap(Alice=PlayerSnapshot(rounds={"Round 1": "Not reported"}, opponents={"Round 1": "Bob"}))
    new = _snap(Alice=PlayerSnapshot(rounds={"Round 1": "Bob won 2-0-0"}, opponents={"Round 1": "Bob"}))
    changes = diff(old, new)
    assert changes[0].positive is False

def test_diff_bye():
    old = _snap(Alice=PlayerSnapshot(rounds={"Round 1": "Not reported"}, opponents={"Round 1": "BYE"}))
    new = _snap(
        Alice=PlayerSnapshot(rounds={"Round 1": "Alice was awarded a bye"}, opponents={"Round 1": "BYE"})
    )
    changes = diff(old, new)
    assert changes[0].positive is True

def test_diff_new_pairing():
    old = _snap(Alice=PlayerSnapshot(rounds={}))
    new = _snap(Alice=PlayerSnapshot(
        rounds={"Round 1": "Not reported"},
        opponents={"Round 1": "Bob"},
    ))
    changes = diff(old, new)
    assert len(changes) == 1
    assert "paired" in changes[0].description
    assert changes[0].positive is None

def test_diff_rank_improvement():
    old = _snap(Alice=PlayerSnapshot(rank=10))
    new = _snap(Alice=PlayerSnapshot(rank=5))
    changes = diff(old, new)
    assert any("▲" in c.description and c.positive is True for c in changes)

def test_diff_rank_drop():
    old = _snap(Alice=PlayerSnapshot(rank=5))
    new = _snap(Alice=PlayerSnapshot(rank=10))
    changes = diff(old, new)
    assert any("▼" in c.description and c.positive is False for c in changes)

def test_diff_rank_unchanged():
    ps = PlayerSnapshot(rank=5)
    assert diff(_snap(Alice=ps), _snap(Alice=ps)) == []

def test_diff_multiple_players():
    old = _snap(
        Alice=PlayerSnapshot(rounds={"Round 1": "Not reported"}, opponents={"Round 1": "X"}),
        Bob=PlayerSnapshot(rounds={"Round 1": "Not reported"}, opponents={"Round 1": "Y"}),
    )
    new = _snap(
        Alice=PlayerSnapshot(rounds={"Round 1": "2-0"}, opponents={"Round 1": "X"}),
        Bob=PlayerSnapshot(rounds={"Round 1": "0-2"}, opponents={"Round 1": "Y"}),
    )
    changes = diff(old, new)
    players = {c.player for c in changes}
    assert players == {"Alice", "Bob"}

def test_diff_new_player_in_snapshot():
    old = _snap()
    new = _snap(Alice=PlayerSnapshot(
        rounds={"Round 1": "Not reported"},
        opponents={"Round 1": "Bob"},
    ))
    changes = diff(old, new)
    assert len(changes) == 1
    assert changes[0].player == "Alice"


# _post_all/get_matches pagination behavior is now covered by
# tests/test_melee_client.py — this module only tests tracker's own domain
# logic (fetch_snapshot/diff), with the underlying HTTP calls mocked at
# melee_tiebreakers.melee_client._get/_post.

# ── fetch_snapshot (mocked HTTP) ───────────────────────────────────────────────

ROUNDS_HTML = """
<html><body>
  <div id="pairings-round-selector-container">
    <button data-id="1001" data-name="Round 1" data-is-started="true">Round 1</button>
    <button data-id="1002" data-name="Round 2" data-is-started="false">Round 2</button>
  </div>
</body></html>
"""

MATCHES_RESPONSE = {
    "data": [{
        "Competitors": [
            {"Team": {"Players": [{"Username": "Alice"}]}},
            {"Team": {"Players": [{"Username": "Bob"}]}},
        ],
        "ResultString": "2-0",
    }]
}

STANDINGS_RESPONSE = {
    "data": [{
        "Rank": 3,
        "MatchRecord": "1-0",
        "GameRecord": "2-0",
        "Points": 3,
        "OpponentMatchWinPercentage": 0.667,
        "Team": {"Players": [{"Username": "Alice"}]},
    }]
}


def _patch_http(html=ROUNDS_HTML, matches=MATCHES_RESPONSE, standings=STANDINGS_RESPONSE):
    """Patch tracker._get and tracker._post with canned responses."""
    get_mock = AsyncMock(return_value=html)
    post_mock = AsyncMock(side_effect=[matches, standings])
    return patch(_MC_GET, get_mock), patch(_MC_POST, post_mock)


async def test_fetch_snapshot_happy_path():
    get_p, post_p = _patch_http()
    with get_p, post_p:
        snap = await fetch_snapshot(tournament_id=430189, players=["Alice"])
    alice = snap.players.get("Alice")
    assert alice is not None
    assert alice.rounds.get("Round 1") == "2-0"
    assert alice.opponents.get("Round 1") == "Bob"
    assert alice.rank == 3
    assert alice.record == "1-0"
    assert alice.points == 3


async def test_fetch_snapshot_no_started_rounds():
    html = """
    <html><body>
      <div id="pairings-round-selector-container">
        <button data-id="1001" data-name="Round 1" data-is-started="false">Round 1</button>
      </div>
    </body></html>
    """
    with patch(_MC_GET, AsyncMock(return_value=html)):
        snap = await fetch_snapshot(tournament_id=1, players=["Alice"])
    assert snap.players == {}
    assert snap.error == "No rounds have started yet."


async def test_fetch_snapshot_player_not_in_tournament():
    get_p, post_p = _patch_http()
    with get_p, post_p:
        snap = await fetch_snapshot(tournament_id=430189, players=["Charlie"])
    charlie = snap.players.get("Charlie")
    assert charlie is not None
    assert charlie.rounds == {}
    assert charlie.rank is None


async def test_fetch_snapshot_http_error_sets_error():
    with patch(_MC_GET, AsyncMock(side_effect=Exception("connection refused"))):
        snap = await fetch_snapshot(tournament_id=1, players=["Alice"])
    assert snap.players == {}
    assert "connection refused" in snap.error


async def test_fetch_snapshot_missing_round_selector():
    with patch(_MC_GET, AsyncMock(return_value="<html><body>no selector</body></html>")):
        snap = await fetch_snapshot(tournament_id=1, players=["Alice"])
    assert snap.players == {}
    assert snap.error is not None

async def test_fetch_snapshot_last_round_not_final():
    # ROUNDS_HTML has Round 1 (started) + Round 2 (not started) — not finals
    get_p, post_p = _patch_http()
    with get_p, post_p:
        snap = await fetch_snapshot(tournament_id=430189, players=["Alice"])
    assert snap.last_round_is_final is False

async def test_fetch_snapshot_last_round_is_final():
    html = """
    <html><body>
      <div id="pairings-round-selector-container">
        <button data-id="1" data-name="Semifinals" data-is-started="true">Semifinals</button>
        <button data-id="2" data-name="Finals" data-is-started="true">Finals</button>
      </div>
    </body></html>
    """
    matches = {"data": []}
    standings = {"data": []}
    with patch(_MC_GET, AsyncMock(return_value=html)):
        with patch(_MC_POST, AsyncMock(side_effect=[matches, matches, standings])):
            snap = await fetch_snapshot(tournament_id=1, players=["Alice"])
    assert snap.last_round_is_final is True
    assert snap.rounds_started == 2


# ── fetch_snapshot with cached_rounds ─────────────────────────────────────────

TWO_ROUNDS_HTML = """
<html><body>
  <div id="pairings-round-selector-container">
    <button data-id="1001" data-name="Round 1" data-is-started="true">Round 1</button>
    <button data-id="1002" data-name="Round 2" data-is-started="true">Round 2</button>
  </div>
</body></html>
"""

MATCHES_R2 = {
    "data": [{
        "Competitors": [
            {"Team": {"Players": [{"Username": "Alice"}]}},
            {"Team": {"Players": [{"Username": "Charlie"}]}},
        ],
        "ResultString": "2-1",
    }]
}


async def test_fetch_snapshot_skips_cached_round():
    # Round 1 is fully cached for Alice; only Round 2 + standings should be fetched.
    cached = {"Alice": {"Round 1": ("2-0", "Bob")}}
    post_mock = AsyncMock(side_effect=[MATCHES_R2, {"data": []}])  # R2 matches + standings

    with patch(_MC_GET, AsyncMock(return_value=TWO_ROUNDS_HTML)):
        with patch(_MC_POST, post_mock):
            snap = await fetch_snapshot(tournament_id=1, players=["Alice"], cached_rounds=cached)

    assert post_mock.call_count == 2  # NOT 3 (Round 1 skipped)
    alice = snap.players["Alice"]
    assert alice.rounds["Round 1"] == "2-0"    # from cache
    assert alice.opponents["Round 1"] == "Bob"  # from cache
    assert alice.rounds["Round 2"] == "2-1"    # from API


async def test_fetch_snapshot_fetches_round_when_any_player_missing():
    # Two players tracked; only Alice has Round 1 cached — Bob does not.
    # Round 1 must still be fetched so Bob's result can be filled in.
    cached = {"Alice": {"Round 1": ("2-0", "Bob")}}
    r1_matches = {
        "data": [{
            "Competitors": [
                {"Team": {"Players": [{"Username": "Bob"}]}},
                {"Team": {"Players": [{"Username": "Opponent"}]}},
            ],
            "ResultString": "2-0",
        }]
    }
    r2_matches = {"data": []}
    post_mock = AsyncMock(side_effect=[r1_matches, r2_matches, {"data": []}])

    with patch(_MC_GET, AsyncMock(return_value=TWO_ROUNDS_HTML)):
        with patch(_MC_POST, post_mock):
            snap = await fetch_snapshot(tournament_id=1, players=["Alice", "Bob"], cached_rounds=cached)

    # Round 1 was fetched because Bob lacked a cached entry
    assert post_mock.call_count == 3  # R1 + R2 + standings
    bob = snap.players["Bob"]
    assert bob.rounds["Round 1"] == "2-0"


async def test_fetch_snapshot_no_cache_unchanged():
    # Passing no cached_rounds behaves identically to the original happy-path test.
    get_p, post_p = _patch_http()
    with get_p, post_p:
        snap = await fetch_snapshot(tournament_id=430189, players=["Alice"])
    assert snap.players["Alice"].rounds["Round 1"] == "2-0"


# ── fetch_snapshot: tournament facts come from the same page load ──────────────

async def test_fetch_snapshot_carries_page_facts_from_one_page_load():
    import datetime
    import pathlib
    html = (pathlib.Path(__file__).parent / "fixtures" / "tournament_view.html").read_text()
    get_mock = AsyncMock(return_value=html)
    with patch(_MC_GET, get_mock), patch(_MC_POST, AsyncMock(return_value={"data": []})):
        snap = await fetch_snapshot(tournament_id=403891, players=["Alice"])
    assert get_mock.call_count == 1
    assert snap.name == "2026 Galactic Championship Last Chance Qualifier"
    assert snap.start_time == datetime.datetime(2026, 7, 24, 16, 0, tzinfo=datetime.UTC)
    assert snap.status == "Ended"
    assert snap.ended is True


async def test_fetch_snapshot_without_status_is_not_ended():
    get_p, post_p = _patch_http()
    with get_p, post_p:
        snap = await fetch_snapshot(tournament_id=1, players=["Alice"])
    assert snap.status is None
    assert snap.ended is False
