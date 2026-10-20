import json
from pathlib import Path

from melee_tiebreakers.standings import Standing, find_standing, parse_standings, player_names, tied_groups

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "round_standings.json").read_text()
)


# ── parse_standings (against a real melee.gg response) ─────────────────────────

def test_parse_standings_row_count():
    assert len(parse_standings(FIXTURE["data"])) == 5

def test_parse_standings_first_row_fields():
    s = parse_standings(FIXTURE["data"])[0]
    assert s.rank == 1
    assert s.players == ["IvoryTiger71"]
    assert s.match_record == "7-0-0"
    assert s.game_record == "14-5-0"
    assert s.points == 21
    assert s.opponent_match_win_pct == 0.755102
    assert s.team_game_win_pct == 0.7368421
    assert s.opponent_game_win_pct == 0.7048077
    assert s.final_tiebreaker == 4042125.0
    assert s.opponent_count == 0
    assert s.team_id == 4042125

def test_parse_standings_preserves_rank_order():
    ranks = [s.rank for s in parse_standings(FIXTURE["data"])]
    assert ranks == [1, 2, 3, 4, 5]


# ── player_names ────────────────────────────────────────────────────────────────

def test_player_names_username_only():
    comp = {"Team": {"Players": [{"Username": "MistyComet"}]}}
    assert player_names(comp) == ["MistyComet"]

def test_player_names_includes_display_name_when_different():
    comp = {"Team": {"Players": [{"Username": "MistyComet", "DisplayName": "Yael Millbrook"}]}}
    names = player_names(comp)
    assert "MistyComet" in names
    assert "Yael Millbrook" in names

def test_player_names_no_duplicate_when_same():
    comp = {"Team": {"Players": [{"Username": "Alice", "DisplayName": "Alice"}]}}
    assert player_names(comp) == ["Alice"]

def test_player_names_empty_team():
    assert player_names({"Team": {}}) == ["?"]


# ── find_standing ────────────────────────────────────────────────────────────────

def test_find_standing_found():
    standings = parse_standings(FIXTURE["data"])
    s = find_standing(standings, "ivorytiger71")
    assert s is not None
    assert s.rank == 1

def test_find_standing_not_found():
    standings = parse_standings(FIXTURE["data"])
    assert find_standing(standings, "nobody") is None


# ── tied_groups ───────────────────────────────────────────────────────────────────

def test_tied_groups_all_same_points():
    # Every row in the fixture is on 21 points (undefeated Swiss) — one big tie.
    standings = parse_standings(FIXTURE["data"])
    groups = tied_groups(standings)
    assert len(groups) == 1
    assert len(groups[0]) == 5

def test_tied_groups_splits_on_points():
    def _standing(rank: int, points: int) -> Standing:
        return Standing(
            rank=rank, players=[f"p{rank}"], match_record=None, game_record=None,
            points=points, opponent_match_win_pct=None, team_game_win_pct=None,
            opponent_game_win_pct=None, final_tiebreaker=None, opponent_count=None,
            team_id=None, raw={},
        )

    standings = [_standing(1, 9), _standing(2, 9), _standing(3, 6)]
    groups = tied_groups(standings)
    assert [len(g) for g in groups] == [2, 1]
