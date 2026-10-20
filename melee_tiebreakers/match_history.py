"""Raw SWU match rows -> typed results and a whole-field rematch graph.

melee.gg's GetRoundMatches rows carry a top-level `Competitors` list (each with
its own `TeamId`/`Team`/`GameWins`/`GameByes`), not the `"Teams"` name
requested as a DataTables sort column — the request-column name and the
returned row shape are unrelated. A bye is a match row with a single
competitor (and a non-null `ByeReason`), automatically worth a match win
(SWU Comprehensive Event Guide §5.2.7).
"""

from dataclasses import dataclass, field

from melee_tiebreakers.melee_client import get_matches


@dataclass(frozen=True)
class MatchResult:
    round_id: int
    table_number: int
    competitor_team_ids: tuple[int, ...]   # 1 (bye) or 2
    game_wins: dict[int, int]              # team_id -> GameWins
    game_draws: int
    reported: bool                         # HasResult
    is_bye: bool
    winner_team_id: int | None             # None if draw or unreported
    raw: dict = field(repr=False, compare=False)


def parse_match_row(row: dict) -> MatchResult:
    competitors = row.get("Competitors", [])
    team_ids = tuple(c["TeamId"] for c in competitors)
    is_bye = len(team_ids) == 1
    game_wins = {c["TeamId"]: c.get("GameWins") or 0 for c in competitors}
    reported = bool(row.get("HasResult"))

    winner_team_id: int | None = None
    if reported:
        if is_bye:
            winner_team_id = team_ids[0]
        elif len(team_ids) == 2:
            a, b = team_ids
            if game_wins[a] != game_wins[b]:
                winner_team_id = a if game_wins[a] > game_wins[b] else b

    return MatchResult(
        round_id=row.get("RoundId"),
        table_number=row.get("TableNumber"),
        competitor_team_ids=team_ids,
        game_wins=game_wins,
        game_draws=row.get("GameDraws") or 0,
        reported=reported,
        is_bye=is_bye,
        winner_team_id=winner_team_id,
        raw=row,
    )


def parse_round_matches(rows: list[dict]) -> list[MatchResult]:
    return [parse_match_row(row) for row in rows]


@dataclass(frozen=True)
class RematchGraph:
    played: dict[int, frozenset[int]]   # team_id -> team_ids already played


def build_rematch_graph(completed_rounds: list[list[MatchResult]]) -> RematchGraph:
    played: dict[int, set[int]] = {}
    for round_matches in completed_rounds:
        for m in round_matches:
            if m.is_bye or len(m.competitor_team_ids) != 2:
                continue
            a, b = m.competitor_team_ids
            played.setdefault(a, set()).add(b)
            played.setdefault(b, set()).add(a)
    return RematchGraph(played={team: frozenset(opponents) for team, opponents in played.items()})


def has_played(graph: RematchGraph, a: int, b: int) -> bool:
    return b in graph.played.get(a, frozenset())


async def fetch_full_match_history(
    tournament_id: int,
    completed_round_ids: list[int],
    known_rounds: dict[int, list[dict]] | None = None,
) -> tuple[RematchGraph, dict[int, list[MatchResult]]]:
    """Build the rematch graph for every completed round of a tournament.

    `known_rounds` lets a caller (melee-bot) supply already-fetched raw rows
    for rounds it has cached — only round IDs missing from it trigger a new
    `get_matches` call, since a completed round's matches never change.
    """
    known_rounds = known_rounds or {}
    matches_by_round: dict[int, list[MatchResult]] = {}
    for round_id in completed_round_ids:
        rows = known_rounds.get(round_id)
        if rows is None:
            rows = await get_matches(round_id, tournament_id)
        matches_by_round[round_id] = parse_round_matches(rows)
    graph = build_rematch_graph(list(matches_by_round.values()))
    return graph, matches_by_round
