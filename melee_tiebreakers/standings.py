"""Typed standings/tiebreaker model built on top of melee.gg's raw JSON.

melee.gg's GetRoundStandings rows carry Magic-style Swiss tiebreakers
(opponents' match-win %, your game-win %, opponents' game-win %) plus an
opaque FinalTiebreaker value. `parse_standings` turns those raw dicts into
`Standing` objects so tiebreaker logic can be written against real fields
instead of dict lookups.
"""

from dataclasses import dataclass, field


def player_names(competitor: dict) -> list[str]:
    """All identifying names (Username + DisplayName) for a standings/match entry.

    Includes both forms because ResultStrings on match rows sometimes use the
    display name (e.g. "Yael Millbrook won 2-1-0") even when the standings
    row's Username is "MistyComet".
    """
    players = competitor.get("Team", {}).get("Players", [])
    names: list[str] = []
    for p in players:
        username = p.get("Username")
        displayname = p.get("DisplayName")
        if username:
            names.append(username)
        if displayname and displayname != username:
            names.append(displayname)
    return names or ["?"]


@dataclass(frozen=True)
class Standing:
    rank: int
    players: list[str]
    match_record: str | None
    game_record: str | None
    points: int | None
    opponent_match_win_pct: float | None
    team_game_win_pct: float | None
    opponent_game_win_pct: float | None
    final_tiebreaker: float | None
    opponent_count: int | None
    team_id: int | None
    raw: dict = field(repr=False, compare=False)


def parse_standings(rows: list[dict]) -> list[Standing]:
    """Convert raw GetRoundStandings rows into `Standing` objects, rank order preserved."""
    return [
        Standing(
            rank=row["Rank"],
            players=player_names(row),
            match_record=row.get("MatchRecord"),
            game_record=row.get("GameRecord"),
            points=row.get("Points"),
            opponent_match_win_pct=row.get("OpponentMatchWinPercentage"),
            team_game_win_pct=row.get("TeamGameWinPercentage"),
            opponent_game_win_pct=row.get("OpponentGameWinPercentage"),
            final_tiebreaker=row.get("FinalTiebreaker"),
            opponent_count=row.get("OpponentCount"),
            team_id=row.get("TeamId"),
            raw=row,
        )
        for row in rows
    ]


def find_standing(standings: list[Standing], player: str) -> Standing | None:
    needle = player.lower()
    for s in standings:
        if any(needle in n.lower() for n in s.players):
            return s
    return None


def tied_groups(standings: list[Standing]) -> list[list[Standing]]:
    """Group standings that share the same Points into ranked ties.

    Groups of size 1 (no tie) are included so the return value covers every
    standing exactly once, in rank order.
    """
    groups: list[list[Standing]] = []
    for s in standings:
        if groups and groups[-1][-1].points == s.points:
            groups[-1].append(s)
        else:
            groups.append([s])
    return groups
