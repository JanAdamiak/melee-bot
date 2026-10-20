"""Regression test: pair_final_round must reproduce melee.gg's real posted
final-Swiss-round pairings, including the cascading rematch-avoidance skips
near the top of standings (e.g. rank 1 skipping ranks 2-4 to pair with rank 5
because it had already played all three).

Validated against three real completed SWU tournaments (455838, 393247,
452461). Only the top ~40 ranks are checked: large events run a mid-Swiss
"progression cut" (see docs/research-pairing-and-tiebreakers.md) after which
only the surviving subset continues, so predicting pairings for the whole
original field diverges from reality below that cutoff — but that's well
outside this tool's actual scope (cut sizes of 8/16/32), so the fixtures only
carry the top 40 standings rows.
"""

import json
from pathlib import Path

import pytest

from melee_tiebreakers.match_history import build_rematch_graph, parse_round_matches
from melee_tiebreakers.pairing import pair_final_round
from melee_tiebreakers.standings import parse_standings

FIXTURES = Path(__file__).parent / "fixtures"

TOURNAMENTS = [393247, 452461, 455838]


def _load(tid: int) -> tuple[list, list]:
    """Returns (standings after round 11, actual round-12 match rows)."""
    fixture_dir = FIXTURES / f"tournament_{tid}"
    standings = parse_standings(json.loads((fixture_dir / "standings_round_11.json").read_text()))
    matches = json.loads((fixture_dir / "matches_round_12.json").read_text())
    return standings, matches


def _rematch_graph(tid: int):
    rounds = []
    for i in range(1, 12):  # rounds 1-11
        rows = json.loads((FIXTURES / f"tournament_{tid}" / f"matches_round_{i:02d}.json").read_text())
        rounds.append(parse_round_matches(rows))
    return build_rematch_graph(rounds)


@pytest.mark.parametrize("tid", TOURNAMENTS)
def test_pair_final_round_matches_real_pairings(tid):
    standings, actual_matches = _load(tid)
    graph = _rematch_graph(tid)
    by_id = {s.team_id: s.rank for s in standings}

    pairs = pair_final_round(standings, graph)
    predicted = sorted(
        tuple(sorted((by_id[a], by_id.get(b)), key=lambda x: (x is None, x)))
        for a, b in pairs
    )

    top_ids = {s.team_id for s in standings}
    actual = sorted(
        tuple(sorted(by_id[c["TeamId"]] for c in m["Competitors"]))
        for m in actual_matches
        if all(c["TeamId"] in top_ids for c in m["Competitors"])
    )

    assert predicted == actual


def test_pair_final_round_cascading_skip_393247():
    """Rank 1 (StormyMeadow) skips ranks 2-4 (all prior opponents) to pair
    with rank 5 (SIP) — the clearest real example of the cascading skip
    behavior that distinguishes this algorithm from naive 1v2, 3v4 pairing."""
    standings, _ = _load(393247)
    graph = _rematch_graph(393247)
    by_id = {s.team_id: s.rank for s in standings}

    pairs = pair_final_round(standings, graph)
    rank_pairs = {by_id[a]: by_id.get(b) for a, b in pairs}

    assert rank_pairs[1] == 5
    assert rank_pairs[2] == 3
    assert rank_pairs[4] == 7
