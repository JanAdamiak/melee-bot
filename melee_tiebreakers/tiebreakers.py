"""Recompute SWU's tiebreaker chain from raw match history for a hypothetical
branch, rather than reading melee.gg's own per-round-frozen percentages.

Validated order and formulas (docs/research-pairing-and-tiebreakers.md,
cross-checked against the official SWU Comprehensive Event Guide and real
melee.gg standings): Match Points -> Opponents' Match-Win% -> Team Game-Win%
-> Opponents' Game-Win%, each of the three percentages floored at exactly
1/3 (melee.gg's own help page states this floor as the literal decimal
"0.33%"/"33%" inconsistently — empirically confirmed against real standings
data to actually be 1/3, not 0.33, since 0.33 alone produces a value that
diverges from melee.gg's own posted OMW%/OGW% by a consistent, floor-count-
proportional amount).
`Team.ID` is used as an empirically-confirmed 5th tier (verified exactly
equal to melee.gg's own opaque `FinalTiebreaker` across ~1,479 real standings
rows) — its direction relative to the other four is unverified, since no real
observed tie ever reached it; implemented ascending ("lower is better"),
same as the other tiers.
"""

from dataclasses import dataclass

_FLOOR = 1 / 3


def match_win_pct(match_wins: int, match_draws: int, match_losses: int) -> float:
    matches = match_wins + match_draws + match_losses
    if matches == 0:
        return 0.0
    points = match_wins * 3 + match_draws
    return points / (3 * matches)


def game_win_pct(game_wins: int, game_draws: int, game_losses: int) -> float:
    games = game_wins + game_draws + game_losses
    if games == 0:
        return 0.0
    points = game_wins * 3 + game_draws
    return points / (3 * games)


def floored(pct: float) -> float:
    return max(pct, _FLOOR)


def omw_pct(opponents_match_pct: list[float]) -> float:
    if not opponents_match_pct:
        return 0.0
    return sum(floored(p) for p in opponents_match_pct) / len(opponents_match_pct)


def ogw_pct(opponents_game_pct: list[float]) -> float:
    if not opponents_game_pct:
        return 0.0
    return sum(floored(p) for p in opponents_game_pct) / len(opponents_game_pct)


@dataclass(frozen=True)
class TeamRecord:
    match_wins: int
    match_draws: int
    match_losses: int
    game_wins: int
    game_draws: int
    game_losses: int

    def match_pct(self) -> float:
        return match_win_pct(self.match_wins, self.match_draws, self.match_losses)

    def game_pct(self) -> float:
        return game_win_pct(self.game_wins, self.game_draws, self.game_losses)


def record_from_strings(match_record: str, game_record: str) -> TeamRecord:
    """melee.gg's MatchRecord/GameRecord are both "W-L-D" strings."""
    mw, ml, md = (int(x) for x in match_record.split("-"))
    gw, gl, gd = (int(x) for x in game_record.split("-"))
    return TeamRecord(mw, md, ml, gw, gd, gl)


def apply_match_result(
    record: TeamRecord, own_game_wins: int, opp_game_wins: int, game_draws: int = 0,
) -> TeamRecord:
    """Fold one additional match's game score into a running record.

    A real SWU draw is recorded on melee.gg as own_game_wins=0, opp_game_wins=0,
    game_draws=3 (every game recorded as a draw, regardless of how much was
    actually played) — see match_history.py. A decisive win/loss defaults to
    a 2-0 game score for a hypothetical hypothetical branch (the match-points/
    OMW% tiers this drives are exact regardless of the exact game score; see
    the TGW%/OGW% sensitivity caveat in enumeration.py for where 2-0 vs 2-1
    can matter and is checked)."""
    if own_game_wins > opp_game_wins:
        match_delta = (1, 0, 0)
    elif own_game_wins < opp_game_wins:
        match_delta = (0, 0, 1)
    else:
        match_delta = (0, 1, 0)
    return TeamRecord(
        match_wins=record.match_wins + match_delta[0],
        match_draws=record.match_draws + match_delta[1],
        match_losses=record.match_losses + match_delta[2],
        game_wins=record.game_wins + own_game_wins,
        game_draws=record.game_draws + game_draws,
        game_losses=record.game_losses + opp_game_wins,
    )


@dataclass(frozen=True)
class ProjectedStanding:
    team_id: int
    points: int
    omw_pct: float
    tgw_pct: float
    ogw_pct: float


def rank_all(standings: list[ProjectedStanding]) -> list[ProjectedStanding]:
    """Sort by (points desc, omw desc, tgw desc, ogw desc, team_id asc) —
    Team.ID as tier-5 tiebreak, direction unverified against a real tie."""
    return sorted(
        standings,
        key=lambda s: (-s.points, -s.omw_pct, -s.tgw_pct, -s.ogw_pct, s.team_id),
    )
