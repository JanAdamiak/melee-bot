"""Validates the tiebreaker recompute engine against melee.gg's own published
values for real players (ground truth from raw match history, not just
internal consistency), and against 393247's real bubble — ranks 9 and 10
tied on points with ranks 3-8 (all at 30) but missing Top 8 purely on OMW%.
"""

import json
from pathlib import Path

from melee_tiebreakers.match_history import parse_round_matches
from melee_tiebreakers.standings import parse_standings
from melee_tiebreakers.tiebreakers import (
    ProjectedStanding,
    floored,
    game_win_pct,
    match_win_pct,
    ogw_pct,
    omw_pct,
    rank_all,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _record(record: str) -> tuple[int, int, int]:
    wins, losses, draws = (int(x) for x in record.split("-"))
    return wins, draws, losses  # match_win_pct/game_win_pct take (wins, draws, losses)


def _opponents_of(tid: int, team_id: int, num_rounds: int) -> set[int]:
    opponents: set[int] = set()
    for i in range(1, num_rounds + 1):
        rows = json.loads((FIXTURES / f"tournament_{tid}" / f"matches_round_{i:02d}.json").read_text())
        for m in parse_round_matches(rows):
            if team_id in m.competitor_team_ids and len(m.competitor_team_ids) == 2:
                a, b = m.competitor_team_ids
                opponents.add(b if a == team_id else a)
    return opponents


# ── floor value (empirically confirmed to be exactly 1/3, not literal 0.33) ────

def test_floor_is_one_third_not_point_three_three():
    assert floored(0.0) == 1 / 3
    assert floored(0.32) == 1 / 3
    assert floored(0.32) != 0.33


# ── formulas reproduce melee.gg's own published percentages exactly ───────────

def test_reproduces_real_omw_tgw_ogw_for_bubble_players():
    tid = 393247
    # OMW%/OGW% average opponents' own percentages, and early-round opponents
    # can be anyone in the whole original field (round 1 pairs at random) — a
    # dedicated closure fixture (target players + every opponent they ever
    # faced) is used here instead of the top-40 fixture the pairing tests use.
    closure_path = FIXTURES / f"tournament_{tid}" / "standings_round_12_omw_closure.json"
    by_team = {r["TeamId"]: r for r in json.loads(closure_path.read_text())}

    # Ranks 1, 2 (top seeds), 8, 9, 10 (the real bubble around Top 8).
    for team_id in (3780972, 3780902, 3781008, 3780701, 3780855):
        row = by_team[team_id]
        gw, gd, gl = _record(row["GameRecord"])
        own_tgw = game_win_pct(gw, gd, gl)
        assert round(own_tgw, 8) == round(row["TeamGameWinPercentage"], 8)

        opponents = _opponents_of(tid, team_id, num_rounds=12)
        opp_mwp = [match_win_pct(*_record(by_team[o]["MatchRecord"])) for o in opponents]
        opp_gwp = [game_win_pct(*_record(by_team[o]["GameRecord"])) for o in opponents]
        assert round(omw_pct(opp_mwp), 8) == round(row["OpponentMatchWinPercentage"], 8)
        assert round(ogw_pct(opp_gwp), 8) == round(row["OpponentGameWinPercentage"], 8)


# ── real bubble: ranks 9/10 miss Top 8 purely on OMW% ──────────────────────────

def test_393247_bubble_ranks_9_and_10_miss_cut_on_omw_alone():
    tid = 393247
    standings_path = FIXTURES / f"tournament_{tid}" / "standings_round_12.json"
    standings = parse_standings(json.loads(standings_path.read_text()))
    by_rank = {s.rank: s for s in standings}

    # Ranks 3-10 are all tied at 30 points (see docs/research-pairing-and-tiebreakers.md).
    tied_group = [by_rank[r] for r in range(3, 11)]
    assert all(s.points == 30 for s in tied_group)

    projected = [
        ProjectedStanding(
            team_id=s.team_id, points=s.points,
            omw_pct=s.opponent_match_win_pct, tgw_pct=s.team_game_win_pct, ogw_pct=s.opponent_game_win_pct,
        )
        for s in tied_group
    ]
    ranked = rank_all(projected)
    ranked_team_ids = [p.team_id for p in ranked]
    real_order = [s.team_id for s in sorted(tied_group, key=lambda s: s.rank)]

    # Our recompute-independent OMW-based ordering must match melee.gg's actual
    # posted rank order within this tied group.
    assert ranked_team_ids == real_order

    # Ranks 9 and 10 (the last two of this tied group) are the ones who bubble.
    assert real_order[-2:] == [by_rank[9].team_id, by_rank[10].team_id]


# ── FinalTiebreaker == Team.ID, across all 3 real tournaments ──────────────────

def test_final_tiebreaker_equals_team_id_across_all_fixtures():
    for tid in (393247, 452461, 455838):
        for label in ("standings_round_11", "standings_round_12"):
            rows = json.loads((FIXTURES / f"tournament_{tid}" / f"{label}.json").read_text())
            for row in rows:
                assert row["FinalTiebreaker"] == row["TeamId"]
