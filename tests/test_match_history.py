import json
from pathlib import Path

from melee_tiebreakers.match_history import (
    build_rematch_graph,
    has_played,
    parse_match_row,
    parse_round_matches,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ── parse_match_row (against real melee.gg rows) ───────────────────────────────

def test_parse_decisive_win_row():
    rows = json.loads((FIXTURES / "tournament_393247" / "matches_round_12.json").read_text())
    row = next(r for r in rows if r["ResultString"] and "won 2-0-0" in r["ResultString"])
    m = parse_match_row(row)
    assert m.reported is True
    assert m.is_bye is False
    winner_wins = m.game_wins[m.winner_team_id]
    assert winner_wins == 2
    assert m.winner_team_id is not None


def test_parse_draw_row():
    rows = json.loads((FIXTURES / "tournament_393247" / "matches_round_12.json").read_text())
    row = next(r for r in rows if r["ResultString"] and "Draw" in r["ResultString"])
    m = parse_match_row(row)
    assert m.reported is True
    assert m.winner_team_id is None
    assert m.game_draws == 3
    assert all(w == 0 for w in m.game_wins.values())


def test_parse_bye_row():
    # Round 3 of 393247 contains a real bye (single competitor).
    rows = json.loads((FIXTURES / "tournament_393247" / "matches_round_03.json").read_text())
    row = next(r for r in rows if len(r["Competitors"]) == 1)
    m = parse_match_row(row)
    assert m.is_bye is True
    assert m.winner_team_id == m.competitor_team_ids[0]


# ── build_rematch_graph / has_played (against full real tournament history) ───

def _load_tournament(tid: int, num_rounds: int) -> list[list]:
    rounds = []
    for i in range(1, num_rounds + 1):
        rows = json.loads((FIXTURES / f"tournament_{tid}" / f"matches_round_{i:02d}.json").read_text())
        rounds.append(parse_round_matches(rows))
    return rounds


def test_rematch_graph_reflects_real_history():
    completed = _load_tournament(393247, 11)
    graph = build_rematch_graph(completed)

    # StormyMeadow (3780972) vs CrimsonCricket (3780902) — round 12's top table pairing
    # (see test_pairing.py) is only reachable if neither has played the other yet.
    assert has_played(graph, 3780972, 3780902) is False

    # Sanity: every non-bye pairing in the loaded rounds must show up both ways.
    for round_matches in completed:
        for m in round_matches:
            if m.is_bye or len(m.competitor_team_ids) != 2:
                continue
            a, b = m.competitor_team_ids
            assert has_played(graph, a, b)
            assert has_played(graph, b, a)


def test_has_played_false_for_unrelated_teams():
    graph = build_rematch_graph(_load_tournament(393247, 11))
    assert has_played(graph, 3780972, 999999999) is False
