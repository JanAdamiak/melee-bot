from melee_tiebreakers.enumeration import MatchOutcome, OutstandingMatch, ResolvedBranch
from melee_tiebreakers.summarize import summarize_player
from melee_tiebreakers.tiebreakers import ProjectedStanding

TARGET = 100
FILLER = 999
CUT_SIZE = 1   # only rank 1 makes it — keeps fixtures tiny

OWN_MATCH = OutstandingMatch(TARGET, 200)
OTHER_1 = OutstandingMatch(300, 301, table_number=4)
OTHER_2 = OutstandingMatch(400, 401, table_number=7)


def _standings(target_made_cut: bool) -> list[ProjectedStanding]:
    target = ProjectedStanding(team_id=TARGET, points=0, omw_pct=0, tgw_pct=0, ogw_pct=0)
    filler = ProjectedStanding(team_id=FILLER, points=0, omw_pct=0, tgw_pct=0, ogw_pct=0)
    return [target, filler] if target_made_cut else [filler, target]


def _branch(
    own: MatchOutcome, others: dict[OutstandingMatch, MatchOutcome], made_cut: bool,
) -> ResolvedBranch:
    current = {OWN_MATCH: own, **others}
    return ResolvedBranch(current_branch=current, next_branch=None, final_standings=_standings(made_cut))


# ── clinched / eliminated ───────────────────────────────────────────────────────

def test_clinched_when_every_branch_makes_cut():
    branches = [
        _branch(MatchOutcome.A_WINS, {OTHER_1: o1, OTHER_2: o2}, made_cut=True)
        for o1 in MatchOutcome for o2 in MatchOutcome
    ]
    result = summarize_player(branches, TARGET, CUT_SIZE, OWN_MATCH)
    assert result.clinched is True
    assert result.eliminated is False


def test_eliminated_when_no_branch_makes_cut():
    branches = [
        _branch(MatchOutcome.B_WINS, {OTHER_1: o1, OTHER_2: o2}, made_cut=False)
        for o1 in MatchOutcome for o2 in MatchOutcome
    ]
    result = summarize_player(branches, TARGET, CUT_SIZE, OWN_MATCH)
    assert result.eliminated is True
    assert result.clinched is False


# ── single swinging match ───────────────────────────────────────────────────────

def test_single_swinging_match_identified():
    # Under a DRAW of the player's own match: fails exactly when OTHER_1 draws,
    # regardless of OTHER_2 — OTHER_1 alone should be identified as the swing.
    branches = [
        _branch(MatchOutcome.DRAW, {OTHER_1: o1, OTHER_2: o2}, made_cut=(o1 is not MatchOutcome.DRAW))
        for o1 in MatchOutcome for o2 in MatchOutcome
    ]
    result = summarize_player(branches, TARGET, CUT_SIZE, OWN_MATCH)
    draw_breakdown = next(b for b in result.breakdowns if b.outcome is MatchOutcome.DRAW)
    assert draw_breakdown.made_cut == 6   # 3 outcomes of OTHER_2, for each of the 2 non-draw OTHER_1 outcomes
    assert draw_breakdown.total == 9
    assert draw_breakdown.swinging_matches == [OTHER_1]


# ── two swinging matches (AND case, matching the target UX) ────────────────────

def test_two_swinging_matches_and_case():
    # Fails exactly when BOTH OTHER_1 and OTHER_2 draw.
    branches = [
        _branch(
            MatchOutcome.DRAW, {OTHER_1: o1, OTHER_2: o2},
            made_cut=not (o1 is MatchOutcome.DRAW and o2 is MatchOutcome.DRAW),
        )
        for o1 in MatchOutcome for o2 in MatchOutcome
    ]
    result = summarize_player(branches, TARGET, CUT_SIZE, OWN_MATCH)
    draw_breakdown = next(b for b in result.breakdowns if b.outcome is MatchOutcome.DRAW)
    assert draw_breakdown.made_cut == 8
    assert draw_breakdown.total == 9
    assert set(draw_breakdown.swinging_matches) == {OTHER_1, OTHER_2}


# ── no clean conjunction (degrade to naming candidates without AND/OR) ─────────

def test_no_clean_conjunction_degrades_to_candidate_list():
    # Fails exactly when OTHER_1 and OTHER_2 disagree (an XOR pattern) — not
    # expressible as a single small conjunction of fixed assignments.
    branches = [
        _branch(MatchOutcome.DRAW, {OTHER_1: o1, OTHER_2: o2}, made_cut=(o1 == o2))
        for o1 in MatchOutcome for o2 in MatchOutcome
    ]
    result = summarize_player(branches, TARGET, CUT_SIZE, OWN_MATCH)
    draw_breakdown = next(b for b in result.breakdowns if b.outcome is MatchOutcome.DRAW)
    assert draw_breakdown.made_cut == 3   # o1 == o2 for exactly 3 of the 9 combos
    # degraded path: both matches named as relevant, no clean AND/OR claimed
    assert set(draw_breakdown.swinging_matches) == {OTHER_1, OTHER_2}
