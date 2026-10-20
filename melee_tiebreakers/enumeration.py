"""Exact enumeration of a tournament's remaining outstanding matches.

Every outstanding match has exactly 3 reachable results (A wins / B wins /
draw — SWU points 3/1/0, draws only in Swiss, only by mutual agreement). This
enumerates every combination exactly rather than sampling/estimating one, and
— when checking during the second-to-last Swiss round — additionally
projects the not-yet-paired final round per branch, using `pairing.
pair_final_round` on that branch's own resulting rank order (final-round
pairing is deterministic once standings are known, so this stays exact).

A branch's classification (does the target make the cut?) can occasionally
depend on TGW%/OGW%, which need a *game* score a bare win/loss/draw doesn't
specify (2-0 vs 2-1 differ). `resolve_round` assumes a synthetic 2-0 game
score for a decisive win; `cut_results_for_roster` re-checks any branch whose
classification is decided at the TGW/OGW tier with the opposite extreme (2-1)
and flags disagreement rather than silently picking one — see
`OwnOutcomeBreakdown.game_score_sensitive`.
"""

import itertools
from dataclasses import dataclass, field
from enum import Enum, auto

from melee_tiebreakers.match_history import RematchGraph
from melee_tiebreakers.pairing import pair_final_round
from melee_tiebreakers.standings import Standing
from melee_tiebreakers.tiebreakers import (
    ProjectedStanding,
    TeamRecord,
    apply_match_result,
    ogw_pct,
    omw_pct,
    rank_all,
)

_EMPTY_RECORD = TeamRecord(0, 0, 0, 0, 0, 0)


class MatchOutcome(Enum):
    A_WINS = auto()
    B_WINS = auto()
    DRAW = auto()


def _game_score(outcome: MatchOutcome, decisive_margin: tuple[int, int] = (2, 0)) -> tuple[int, int, int]:
    """(team_a_game_wins, team_b_game_wins, game_draws) for one outcome.

    A real SWU draw is always recorded as 0-0-3 (see match_history.py). A
    decisive win/loss's exact game score is genuinely unknowable from a bare
    win/loss result — `decisive_margin` lets the caller check both extremes.
    """
    winner_games, loser_games = decisive_margin
    if outcome is MatchOutcome.A_WINS:
        return winner_games, loser_games, 0
    if outcome is MatchOutcome.B_WINS:
        return loser_games, winner_games, 0
    return 0, 0, 3


@dataclass(frozen=True)
class OutstandingMatch:
    team_a: int
    team_b: int
    table_number: int | None = None


_MAX_BRANCH_EXPONENT = 12   # 3**12 ~= 531k branches; tune/benchmark before shipping


class TooManyBranchesError(Exception):
    """Caller must show 'too many outstanding matches to enumerate exactly
    yet' and retry next poll as results come in and the count shrinks — never
    fall back to estimation."""

    def __init__(self, count: int):
        super().__init__(f"{count} outstanding matches exceeds the exact-enumeration cap")
        self.count = count


def enumerate_branches(matches: list[OutstandingMatch]) -> list[dict[OutstandingMatch, MatchOutcome]]:
    if len(matches) > _MAX_BRANCH_EXPONENT:
        raise TooManyBranchesError(len(matches))
    combos = itertools.product(MatchOutcome, repeat=len(matches))
    return [dict(zip(matches, combo, strict=True)) for combo in combos]


@dataclass(frozen=True)
class RoundContext:
    """Everything needed to enumerate and resolve one round's outstanding
    matches on top of every team's already-known record.

    `baseline_records` already includes this round's REPORTED matches (if
    any) — only `outstanding` needs a branch assignment. `round_pairing`
    covers the round's whole relevant pairing (reported + outstanding), used
    to extend `opponents_before` with this round's opponent edges for
    OMW%/OGW% purposes.

    OMW%/OGW% average opponents' OWN match/game percentages — including
    opponents from rounds well before this one, most of whom are outside
    whatever narrow band is actually being enumerated/paired (a real field's
    early rounds pair essentially at random, so a tracked player's round-1
    opponent is rarely another tracked player). `fixed_opponent_records`
    supplies those opponents' real, already-known records (their own result
    isn't affected by this round's branches) so OMW%/OGW% average over every
    real opponent instead of silently only the few who happen to also be
    tracked.
    """

    baseline_records: dict[int, TeamRecord]
    opponents_before: dict[int, frozenset[int]]
    round_pairing: list[tuple[int, int]]
    outstanding: list[OutstandingMatch]
    fixed_opponent_records: dict[int, TeamRecord] = field(default_factory=dict)


def _opponents_after(ctx: RoundContext) -> dict[int, frozenset[int]]:
    merged: dict[int, set[int]] = {team: set(opps) for team, opps in ctx.opponents_before.items()}
    for a, b in ctx.round_pairing:
        merged.setdefault(a, set()).add(b)
        merged.setdefault(b, set()).add(a)
    return {team: frozenset(opps) for team, opps in merged.items()}


def resolve_round(
    ctx: RoundContext,
    branch: dict[OutstandingMatch, MatchOutcome],
    decisive_margin: tuple[int, int] = (2, 0),
) -> tuple[dict[int, TeamRecord], list[ProjectedStanding]]:
    records = dict(ctx.baseline_records)
    for m in ctx.outstanding:
        a_gw, b_gw, gd = _game_score(branch[m], decisive_margin)
        records[m.team_a] = apply_match_result(records.get(m.team_a, _EMPTY_RECORD), a_gw, b_gw, gd)
        records[m.team_b] = apply_match_result(records.get(m.team_b, _EMPTY_RECORD), b_gw, a_gw, gd)

    opponents = _opponents_after(ctx)
    standings = []
    for team_id, rec in records.items():
        opp_ids = opponents.get(team_id, frozenset())
        opp_records = [
            records[o] if o in records else ctx.fixed_opponent_records[o]
            for o in opp_ids
            if o in records or o in ctx.fixed_opponent_records
        ]
        opp_match_pcts = [o.match_pct() for o in opp_records]
        opp_game_pcts = [o.game_pct() for o in opp_records]
        standings.append(ProjectedStanding(
            team_id=team_id,
            points=rec.match_wins * 3 + rec.match_draws,
            omw_pct=omw_pct(opp_match_pcts),
            tgw_pct=rec.game_pct(),
            ogw_pct=ogw_pct(opp_game_pcts),
        ))
    return records, rank_all(standings)


def _as_standings(ranked: list[ProjectedStanding]) -> list[Standing]:
    """Minimal Standing objects for pairing.pair_final_round, which only
    reads .rank and .team_id."""
    return [
        Standing(
            rank=i + 1, players=[str(p.team_id)], match_record=None, game_record=None,
            points=p.points, opponent_match_win_pct=p.omw_pct, team_game_win_pct=p.tgw_pct,
            opponent_game_win_pct=p.ogw_pct, final_tiebreaker=None, opponent_count=None,
            team_id=p.team_id, raw={},
        )
        for i, p in enumerate(ranked)
    ]


@dataclass(frozen=True)
class TournamentState:
    current_round: RoundContext
    rematch_graph_before_current: RematchGraph   # opponents_before, as a RematchGraph, for projecting round 2
    project_next_round: bool                     # True when checking the second-to-last Swiss round


def _projected_round_context(
    current_records: dict[int, TeamRecord],
    current_ranked: list[ProjectedStanding],
    opponents_after_current: dict[int, frozenset[int]],
    rematch_graph_after_current: RematchGraph,
    fixed_opponent_records: dict[int, TeamRecord],
) -> RoundContext:
    standings = _as_standings(current_ranked)
    pairs = pair_final_round(standings, rematch_graph_after_current)
    round_pairing = [(a, b) for a, b in pairs if b is not None]
    outstanding = [OutstandingMatch(team_a=a, team_b=b) for a, b in round_pairing]
    return RoundContext(
        baseline_records=current_records,
        opponents_before=opponents_after_current,
        round_pairing=round_pairing,
        outstanding=outstanding,
        fixed_opponent_records=fixed_opponent_records,
    )


def _merge_graphs(a: dict[int, frozenset[int]], b: RematchGraph) -> RematchGraph:
    return RematchGraph(played={
        team: a.get(team, frozenset()) | b.played.get(team, frozenset())
        for team in set(a) | set(b.played)
    })


@dataclass(frozen=True)
class ResolvedBranch:
    current_branch: dict[OutstandingMatch, MatchOutcome]
    next_branch: dict[OutstandingMatch, MatchOutcome] | None
    final_standings: list[ProjectedStanding]


def resolve_all_branches(
    state: TournamentState, decisive_margin: tuple[int, int] = (2, 0),
) -> list[ResolvedBranch]:
    """Every reachable branch of `state`, each fully resolved to final ranked
    standings. When `state.project_next_round` is True, the projected final
    round's pairing/outstanding-match count depends on the current round's
    branch (rank order shifts who plays whom), so it's derived and enumerated
    per current-round branch rather than once up front — each branch's own
    outstanding count is still capped like `enumerate_branches`."""
    current_matches = state.current_round.outstanding
    if len(current_matches) > _MAX_BRANCH_EXPONENT:
        raise TooManyBranchesError(len(current_matches))

    resolved: list[ResolvedBranch] = []
    for current_branch in enumerate_branches(current_matches):
        records, ranked = resolve_round(state.current_round, current_branch, decisive_margin)
        if not state.project_next_round:
            resolved.append(ResolvedBranch(current_branch, None, ranked))
            continue

        opponents_after = _opponents_after(state.current_round)
        merged_graph = _merge_graphs(opponents_after, state.rematch_graph_before_current)
        next_ctx = _projected_round_context(
            records, ranked, opponents_after, merged_graph, state.current_round.fixed_opponent_records,
        )
        for next_branch in enumerate_branches(next_ctx.outstanding):
            _, final_ranked = resolve_round(next_ctx, next_branch, decisive_margin)
            resolved.append(ResolvedBranch(current_branch, next_branch, final_ranked))
    return resolved
