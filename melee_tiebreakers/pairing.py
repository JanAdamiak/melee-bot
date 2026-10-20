"""Final-Swiss-round pairing, validated against real melee.gg tournament data.

For an event cutting to single elimination, the final Swiss round pairs
players against "the player closest to their rank that they have not yet
played" (SWU Comprehensive Event Guide §10.4) — rank-adjacent, not the
random-within-score-group pairing used in every other Swiss round. Confirmed
against three real completed tournaments' actual posted final-round pairings
(including cascading skips near the top of standings caused by earlier
rematches, e.g. rank 1 paired with rank 5 because ranks 2-4 were all repeats).
"""

from melee_tiebreakers.match_history import RematchGraph, has_played
from melee_tiebreakers.standings import Standing


def pair_final_round(
    standings: list[Standing],
    rematch_graph: RematchGraph,
) -> list[tuple[int, int | None]]:
    """Greedy top-down sweep: process players in ascending rank order; pair
    each still-unpaired player with the nearest-ranked still-unpaired player
    they have not already played, scanning downward. A player left with no
    eligible opponent (odd field) gets a bye.

    Returns a list of (team_id_a, team_id_b) pairs, or (team_id, None) for a
    bye, in the order pairs were formed (ascending by the lower-ranked side).
    """
    ordered = sorted(standings, key=lambda s: s.rank)
    unpaired = [s.team_id for s in ordered]
    unpaired_set = set(unpaired)
    pairs: list[tuple[int, int | None]] = []

    for team_id in unpaired:
        if team_id not in unpaired_set:
            continue
        unpaired_set.discard(team_id)
        opponent = next(
            (
                other
                for other in unpaired
                if other in unpaired_set and not has_played(rematch_graph, team_id, other)
            ),
            None,
        )
        if opponent is not None:
            unpaired_set.discard(opponent)
            pairs.append((team_id, opponent))
        else:
            pairs.append((team_id, None))

    return pairs
