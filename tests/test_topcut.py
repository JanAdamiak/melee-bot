from unittest.mock import AsyncMock, patch

import discord
import pytest

import domain.topcut as topcut
from domain.topcut import (
    FetchedData,
    RoundPositionError,
    TopCutSession,
    build_state,
    build_topcut_embed,
    classify_round_position,
    is_bubble,
)
from melee_tiebreakers.enumeration import MatchOutcome, OutstandingMatch
from melee_tiebreakers.match_history import RematchGraph
from melee_tiebreakers.standings import Standing
from melee_tiebreakers.summarize import OwnOutcomeBreakdown, PlayerCutResult


def _round(name: str, started: bool, rid: int = 1) -> dict:
    return {"id": rid, "name": name, "is_started": started}


# ── classify_round_position ──────────────────────────────────────────────────────

def test_classify_last_round():
    rounds = [_round("Round 1", True, 1), _round("Round 2", True, 2), _round("Quarterfinals", False, 3)]
    current, project_next = classify_round_position(rounds)
    assert current["id"] == 2
    assert project_next is False


def test_classify_second_to_last_round():
    rounds = [
        _round("Round 1", True, 1), _round("Round 2", True, 2),
        _round("Round 3", False, 3), _round("Quarterfinals", False, 4),
    ]
    current, project_next = classify_round_position(rounds)
    assert current["id"] == 2
    assert project_next is True


def test_classify_too_early_raises():
    rounds = [
        _round("Round 1", True, 1), _round("Round 2", False, 2),
        _round("Round 3", False, 3), _round("Quarterfinals", False, 4),
    ]
    with pytest.raises(RoundPositionError):
        classify_round_position(rounds)


def test_classify_no_swiss_rounds_raises():
    with pytest.raises(RoundPositionError):
        classify_round_position([_round("Quarterfinals", True, 1)])


def test_classify_swiss_not_started_raises():
    rounds = [_round("Round 1", False, 1)]
    with pytest.raises(RoundPositionError):
        classify_round_position(rounds)


# ── build_state ───────────────────────────────────────────────────────────────

def _standing(team_id: int, rank: int, match_record="1-0-0", game_record="2-0-0") -> Standing:
    return Standing(
        rank=rank, players=[str(team_id)], match_record=match_record, game_record=game_record,
        points=3, opponent_match_win_pct=0.5, team_game_win_pct=0.5, opponent_game_win_pct=0.5,
        final_tiebreaker=team_id, opponent_count=1, team_id=team_id, raw={},
    )


def _match(team_a, team_b, reported, winner=None, table=1):
    from melee_tiebreakers.match_history import MatchResult
    game_wins = {team_a: 2 if winner == team_a else 0, team_b: 2 if winner == team_b else 0}
    return MatchResult(
        round_id=1, table_number=table, competitor_team_ids=(team_a, team_b),
        game_wins=game_wins, game_draws=0, reported=reported,
        is_bye=False, winner_team_id=winner, raw={},
    )


def test_build_state_folds_reported_matches_into_baseline():
    standings = [_standing(1, 1), _standing(2, 2)]
    matches = [_match(1, 2, reported=True, winner=1)]
    data = FetchedData(
        current_round={"id": 99, "name": "Round 5"}, project_next_round=False,
        baseline_standings=standings, current_matches=matches,
        rematch_graph=RematchGraph(played={}),
    )
    state = build_state(data, cut_size=8, tracked_team_ids={1, 2})
    assert state.current_round.outstanding == []
    assert state.current_round.baseline_records[1].match_wins == 2   # 1 from standings + 1 from reported win


def test_build_state_treats_unreported_relevant_match_as_outstanding():
    standings = [_standing(1, 1), _standing(2, 2)]
    matches = [_match(1, 2, reported=False, table=4)]
    data = FetchedData(
        current_round={"id": 99, "name": "Round 5"}, project_next_round=False,
        baseline_standings=standings, current_matches=matches,
        rematch_graph=RematchGraph(played={}),
    )
    state = build_state(data, cut_size=8, tracked_team_ids={1, 2})
    assert state.current_round.outstanding == [OutstandingMatch(team_a=1, team_b=2, table_number=4)]


def test_build_state_excludes_irrelevant_unreported_match():
    # Neither team is tracked nor within cut_size+margin ranks -> not outstanding.
    standings = [_standing(t, t) for t in range(1, 30)]
    matches = [_match(28, 29, reported=False)]
    data = FetchedData(
        current_round={"id": 99, "name": "Round 5"}, project_next_round=False,
        baseline_standings=standings, current_matches=matches,
        rematch_graph=RematchGraph(played={}),
    )
    state = build_state(data, cut_size=8, tracked_team_ids=set())
    assert state.current_round.outstanding == []


# ── is_bubble ─────────────────────────────────────────────────────────────────

def test_is_bubble_true_at_threshold():
    from melee_tiebreakers.enumeration import RoundContext, TournamentState
    ctx = RoundContext(
        baseline_records={}, opponents_before={}, round_pairing=[],
        outstanding=[OutstandingMatch(1, 2)] * topcut._BUBBLE_THRESHOLD,
    )
    state = TournamentState(
        current_round=ctx, rematch_graph_before_current=RematchGraph({}), project_next_round=False,
    )
    assert is_bubble(state) is True


def test_is_bubble_false_above_threshold():
    from melee_tiebreakers.enumeration import RoundContext, TournamentState
    ctx = RoundContext(
        baseline_records={}, opponents_before={}, round_pairing=[],
        outstanding=[OutstandingMatch(i, i + 1) for i in range(topcut._BUBBLE_THRESHOLD + 1)],
    )
    state = TournamentState(
        current_round=ctx, rematch_graph_before_current=RematchGraph({}), project_next_round=False,
    )
    assert is_bubble(state) is False


# ── build_topcut_embed ──────────────────────────────────────────────────────────

def _session(**overrides) -> TopCutSession:
    defaults = dict(
        guild_id=1, channel_id=1, message_id=2, tournament_id=430189, cut_size=8, started_by="user#1",
    )
    defaults.update(overrides)
    return TopCutSession(**defaults)


def test_embed_shows_error():
    session = _session(error="Swiss hasn't started yet.")
    embed = build_topcut_embed(session, {})
    assert "Swiss hasn't started yet." in embed.description


def test_embed_clinched_player():
    result = PlayerCutResult(team_id=1, own_match=None, breakdowns=[], clinched=True, eliminated=False)
    session = _session(results={"Alice": result})
    embed = build_topcut_embed(session, {})
    assert embed.fields[0].name == "Alice"
    assert "Clinched" in embed.fields[0].value


def test_embed_eliminated_player():
    result = PlayerCutResult(team_id=1, own_match=None, breakdowns=[], clinched=False, eliminated=True)
    session = _session(results={"Bob": result})
    embed = build_topcut_embed(session, {})
    assert "Eliminated" in embed.fields[0].value


def test_embed_breakdown_with_swinging_match():
    om = OutstandingMatch(team_a=10, team_b=11, table_number=4)
    breakdown = OwnOutcomeBreakdown(
        outcome=MatchOutcome.DRAW, made_cut=51, total=64, swinging_matches=[om], game_score_sensitive=False,
    )
    result = PlayerCutResult(
        team_id=1, own_match=OutstandingMatch(1, 2), breakdowns=[breakdown],
        clinched=False, eliminated=False,
    )
    session = _session(results={"Alice": result})
    embed = build_topcut_embed(session, {"Alice": "AliceDisplay"})
    assert embed.fields[0].name == "AliceDisplay"
    assert "DRAW" in embed.fields[0].value
    assert "51/64" in embed.fields[0].value
    assert "Table 4" in embed.fields[0].value


def test_embed_color_bubble():
    session = _session(bubble=True)
    embed = build_topcut_embed(session, {})
    assert embed.color == discord.Color.gold()


def test_embed_color_concluded():
    session = _session(concluded=True)
    embed = build_topcut_embed(session, {})
    assert embed.color == discord.Color.greyple()


def test_embed_color_normal():
    session = _session()
    embed = build_topcut_embed(session, {})
    assert embed.color == discord.Color.blue()


# ── refresh_session (mocked HTTP, real isolated DB) ─────────────────────────────

_MC_GET = "melee_tiebreakers.melee_client._get"
_MC_POST = "melee_tiebreakers.melee_client._post"

_ROUNDS_HTML = """
<html><body>
  <div id="pairings-round-selector-container">
    <button data-id="1001" data-name="Round 1" data-is-started="true">Round 1</button>
    <button data-id="1002" data-name="Round 2" data-is-started="true">Round 2</button>
  </div>
</body></html>
"""


def _standing_row(team_id: int, rank: int, match_record: str, game_record: str) -> dict:
    return {
        "Rank": rank, "TeamId": team_id,
        "Team": {"ID": team_id, "Players": [{"Username": f"Player{team_id}", "DisplayName": None}]},
        "Points": 3, "MatchRecord": match_record, "GameRecord": game_record,
        "OpponentMatchWinPercentage": 0.5, "TeamGameWinPercentage": 0.5, "OpponentGameWinPercentage": 0.5,
        "FinalTiebreaker": team_id, "OpponentCount": 1,
    }


def _match_row(
    round_id: int, table: int, team_a: int, team_b: int, reported: bool, winner: int | None = None,
) -> dict:
    return {
        "RoundId": round_id, "TableNumber": table, "GameDraws": 0, "HasResult": reported,
        "ResultString": "2-0" if reported else None,
        "Competitors": [
            {"TeamId": team_a, "Team": {"ID": team_a, "Players": [{"Username": f"Player{team_a}"}]},
             "GameWins": 2 if winner == team_a else 0, "GameByes": 0},
            {"TeamId": team_b, "Team": {"ID": team_b, "Players": [{"Username": f"Player{team_b}"}]},
             "GameWins": 2 if winner == team_b else 0, "GameByes": 0},
        ],
    }


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    import domain.db as db
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))


async def test_refresh_session_end_to_end():
    import domain.db as db

    await db.init_db()
    await db.add_player(1, "Player1", "user#1")

    round1_matches = {"data": [_match_row(1001, 1, 1, 2, reported=True, winner=1)]}
    round1_standings = {"data": [
        _standing_row(1, 1, "1-0-0", "2-0-0"),
        _standing_row(2, 2, "0-1-0", "0-2-0"),
    ]}
    round2_matches = {"data": [_match_row(1002, 1, 1, 3, reported=False)]}

    get_mock = AsyncMock(return_value=_ROUNDS_HTML)
    post_mock = AsyncMock(side_effect=[round1_matches, round1_standings, round2_matches])

    with patch(_MC_GET, get_mock), patch(_MC_POST, post_mock):
        session = TopCutSession(
            guild_id=1, channel_id=1, message_id=2, tournament_id=430189, cut_size=8, started_by="user#1",
        )
        await topcut.refresh_session(session)

    assert session.error is None
    assert "Player1" in session.results
    result = session.results["Player1"]
    assert result.clinched or result.eliminated or result.breakdowns

    # round 1's matches should now be cached
    cache = await db.load_round_match_cache(430189)
    assert 1001 in cache
