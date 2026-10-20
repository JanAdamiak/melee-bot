import json
from pathlib import Path

import pytest

from melee_tiebreakers.enumeration import (
    MatchOutcome,
    OutstandingMatch,
    RoundContext,
    TooManyBranchesError,
    enumerate_branches,
    resolve_round,
)
from melee_tiebreakers.match_history import parse_round_matches
from melee_tiebreakers.standings import parse_standings
from melee_tiebreakers.tiebreakers import TeamRecord, record_from_strings

FIXTURES = Path(__file__).parent / "fixtures"


# ── enumerate_branches ──────────────────────────────────────────────────────────

def test_enumerate_branches_small_case():
    matches = [OutstandingMatch(1, 2), OutstandingMatch(3, 4)]
    branches = enumerate_branches(matches)
    assert len(branches) == 9   # 3^2
    # every branch assigns exactly one outcome to each match
    assert all(set(b.keys()) == set(matches) for b in branches)
    # every combination of outcomes is reachable exactly once
    seen = {tuple(b[m] for m in matches) for b in branches}
    assert len(seen) == 9


def test_enumerate_branches_empty():
    assert enumerate_branches([]) == [{}]


def test_too_many_branches_raises_at_cap():
    matches = [OutstandingMatch(i, i + 1) for i in range(0, 26, 2)]  # 13 matches
    with pytest.raises(TooManyBranchesError):
        enumerate_branches(matches)


def test_exactly_at_cap_does_not_raise():
    matches = [OutstandingMatch(i, i + 1) for i in range(0, 24, 2)]  # 12 matches
    assert len(enumerate_branches(matches)) == 3 ** 12


# ── resolve_round: hand-computed small case ────────────────────────────────────

def test_resolve_round_hand_computed():
    # Two players (1, 2) both at 0 points enter a single outstanding match.
    ctx = RoundContext(
        baseline_records={1: TeamRecord(0, 0, 0, 0, 0, 0), 2: TeamRecord(0, 0, 0, 0, 0, 0)},
        opponents_before={},
        round_pairing=[(1, 2)],
        outstanding=[OutstandingMatch(1, 2)],
    )
    m = ctx.outstanding[0]

    _, win = resolve_round(ctx, {m: MatchOutcome.A_WINS})
    by_id = {s.team_id: s for s in win}
    assert by_id[1].points == 3
    assert by_id[2].points == 0

    _, draw = resolve_round(ctx, {m: MatchOutcome.DRAW})
    by_id = {s.team_id: s for s in draw}
    assert by_id[1].points == 1
    assert by_id[2].points == 1


# ── real cross-round validation: 393247 round 11 -> round 12 ──────────────────
# Round 12 is the last Swiss round before 393247's Quarterfinals cut, so this
# is exactly the real second-to-last/last transition the tool is built for.

def test_resolve_round_reproduces_real_points_393247():
    fixture_dir = FIXTURES / "tournament_393247"
    r11 = parse_standings(json.loads((fixture_dir / "standings_round_11.json").read_text()))
    r12 = parse_standings(json.loads((fixture_dir / "standings_round_12.json").read_text()))
    r11_by_id = {s.team_id: s for s in r11}
    r12_by_id = {s.team_id: s for s in r12}

    top10 = sorted(r11, key=lambda s: s.rank)[:10]
    top10_ids = {s.team_id for s in top10}

    matches = parse_round_matches(json.loads((fixture_dir / "matches_round_12.json").read_text()))
    relevant = [
        m for m in matches
        if len(m.competitor_team_ids) == 2 and set(m.competitor_team_ids) <= top10_ids
    ]
    assert len(relevant) == 5   # 5 pairs among the top 10 (see test_pairing.py)

    baseline = {s.team_id: record_from_strings(s.match_record, s.game_record) for s in top10}
    round_pairing = [tuple(m.competitor_team_ids) for m in relevant]
    outstanding = [OutstandingMatch(*m.competitor_team_ids) for m in relevant]
    ctx = RoundContext(
        baseline_records=baseline, opponents_before={}, round_pairing=round_pairing, outstanding=outstanding,
    )

    # Branch = the REAL observed outcome for each of these 5 matches.
    branch = {}
    for m, om in zip(relevant, outstanding, strict=True):
        if m.winner_team_id is None:
            branch[om] = MatchOutcome.DRAW
        elif m.winner_team_id == om.team_a:
            branch[om] = MatchOutcome.A_WINS
        else:
            branch[om] = MatchOutcome.B_WINS

    _, ranked = resolve_round(ctx, branch)
    by_id = {s.team_id: s for s in ranked}

    for team_id in top10_ids:
        assert by_id[team_id].points == r12_by_id[team_id].points, (
            f"team {team_id}: expected {r12_by_id[team_id].points} pts "
            f"(rank {r11_by_id[team_id].rank} -> {r12_by_id[team_id].rank}), "
            f"got {by_id[team_id].points}"
        )
