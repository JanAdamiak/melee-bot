"""Turns resolved branches into the target UX: for each tracked player,
broken down by *their own* outstanding match's outcome, each with an exact
fraction of branches where they make the cut, naming the specific other
match(es) that swing that fraction — not a bare percentage, not a full
branch dump.
"""

from dataclasses import dataclass
from itertools import combinations, product

from melee_tiebreakers.enumeration import (
    MatchOutcome,
    OutstandingMatch,
    ResolvedBranch,
    TournamentState,
    resolve_all_branches,
)

_MAX_CONJUNCTION_SIZE = 3
_MAX_CANDIDATE_MATCHES = 12   # beyond this, skip the clean-conjunction search and degrade


@dataclass(frozen=True)
class OwnOutcomeBreakdown:
    outcome: MatchOutcome | None   # None when the player has no live match this round
    made_cut: int
    total: int
    swinging_matches: list[OutstandingMatch]   # empty if clinched/eliminated outright
    game_score_sensitive: bool


@dataclass(frozen=True)
class PlayerCutResult:
    team_id: int
    own_match: OutstandingMatch | None
    breakdowns: list[OwnOutcomeBreakdown]
    clinched: bool
    eliminated: bool


def _branch_outcomes(rb: ResolvedBranch) -> dict[OutstandingMatch, MatchOutcome]:
    merged = dict(rb.current_branch)
    if rb.next_branch:
        merged.update(rb.next_branch)
    return merged


def _made_cut(rb: ResolvedBranch, target_team_id: int, cut_size: int) -> bool | None:
    for i, s in enumerate(rb.final_standings):
        if s.team_id == target_team_id:
            return i < cut_size
    return None   # target absent from final standings — shouldn't happen, treated as unknown


def _find_swinging_matches(
    group: list[ResolvedBranch],
    made_cut_flags: list[bool],
    own_match: OutstandingMatch | None,
) -> list[OutstandingMatch]:
    failing = [i for i, ok in enumerate(made_cut_flags) if not ok]
    if not failing:
        return []

    all_outcomes = [_branch_outcomes(rb) for rb in group]
    candidate_matches = sorted(
        {m for outcomes in all_outcomes for m in outcomes if m != own_match},
        key=lambda m: (m.table_number is None, m.table_number, m.team_a, m.team_b),
    )
    failing_set = set(failing)

    if len(candidate_matches) <= _MAX_CANDIDATE_MATCHES:
        for size in range(1, _MAX_CONJUNCTION_SIZE + 1):
            for combo in combinations(candidate_matches, size):
                for outcome_combo in product(MatchOutcome, repeat=size):
                    condition = dict(zip(combo, outcome_combo, strict=True))
                    matching = {
                        i for i, outcomes in enumerate(all_outcomes)
                        if all(outcomes.get(m) == o for m, o in condition.items())
                    }
                    if matching == failing_set:
                        return list(combo)

    # No clean conjunction found (or too many candidates to search) — degrade
    # to naming every match whose outcome actually varies between a passing
    # and a failing branch, without claiming a clean AND/OR story.
    varying = [
        m for m in candidate_matches
        if len({outcomes.get(m) for outcomes in all_outcomes}) > 1
    ]
    return varying


def summarize_player(
    resolved_branches: list[ResolvedBranch],
    target_team_id: int,
    cut_size: int,
    own_match: OutstandingMatch | None,
) -> PlayerCutResult:
    groups: dict[MatchOutcome | None, list[ResolvedBranch]] = {}
    for rb in resolved_branches:
        outcome = _branch_outcomes(rb).get(own_match) if own_match else None
        groups.setdefault(outcome, []).append(rb)

    breakdowns: list[OwnOutcomeBreakdown] = []
    all_made_cut: list[bool] = []
    for outcome, group in groups.items():
        # game_score_sensitive is always False here — cut_results_for_roster
        # fills in the real value by comparing against a second resolution
        # pass at the opposite decisive-game-score extreme.
        made_cut_flags = [bool(_made_cut(rb, target_team_id, cut_size)) for rb in group]
        all_made_cut.extend(made_cut_flags)
        swinging = _find_swinging_matches(group, made_cut_flags, own_match)
        breakdowns.append(OwnOutcomeBreakdown(
            outcome=outcome,
            made_cut=sum(made_cut_flags),
            total=len(made_cut_flags),
            swinging_matches=swinging,
            game_score_sensitive=False,
        ))

    return PlayerCutResult(
        team_id=target_team_id,
        own_match=own_match,
        breakdowns=breakdowns,
        clinched=all(all_made_cut),
        eliminated=not any(all_made_cut),
    )


def cut_results_for_roster(
    state: TournamentState,
    target_team_ids: list[int],
    cut_size: int,
) -> dict[int, PlayerCutResult]:
    """Enumerate branches ONCE, resolve each ONCE, derive every tracked
    player's result from that single pass (not O(branches * roster))."""
    resolved = resolve_all_branches(state)
    resolved_high = resolve_all_branches(state, decisive_margin=(2, 1))

    own_match_by_team: dict[int, OutstandingMatch | None] = {}
    for team_id in target_team_ids:
        own_match_by_team[team_id] = next(
            (m for m in state.current_round.outstanding if team_id in (m.team_a, m.team_b)),
            None,
        )

    results: dict[int, PlayerCutResult] = {}
    for team_id in target_team_ids:
        own_match = own_match_by_team[team_id]
        result = summarize_player(resolved, team_id, cut_size, own_match)

        def _own_outcome(rb: ResolvedBranch) -> MatchOutcome | None:
            return _branch_outcomes(rb).get(own_match) if own_match else None

        # TGW%/OGW%-tier sensitivity: does the alternate decisive game score
        # (2-1 instead of 2-0) flip any branch's classification?
        sensitive_breakdowns = []
        for breakdown in result.breakdowns:
            group_low = [rb for rb in resolved if _own_outcome(rb) == breakdown.outcome]
            group_high = [rb for rb in resolved_high if _own_outcome(rb) == breakdown.outcome]
            flags_low = [_made_cut(rb, team_id, cut_size) for rb in group_low]
            flags_high = [_made_cut(rb, team_id, cut_size) for rb in group_high]
            sensitive = flags_low != flags_high
            sensitive_breakdowns.append(OwnOutcomeBreakdown(
                outcome=breakdown.outcome, made_cut=breakdown.made_cut, total=breakdown.total,
                swinging_matches=breakdown.swinging_matches, game_score_sensitive=sensitive,
            ))
        results[team_id] = PlayerCutResult(
            team_id=result.team_id, own_match=result.own_match,
            breakdowns=sensitive_breakdowns, clinched=result.clinched, eliminated=result.eliminated,
        )

    return results
