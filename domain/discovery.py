"""Tournament discovery: find tournaments that roster players have entered.

A scan asks melee.gg for each roster player's tournaments, then works out per
server which of them are worth suggesting. This module holds the logic and
the suggestion embed; the Discord plumbing (posting, buttons) lives in
bot.py and commands/discovery.py.
"""

import datetime
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

import discord

from melee_tiebreakers.melee_client import PlayerTournament

log = logging.getLogger(__name__)

SCAN_INTERVAL_HOURS = 3
# Suggest tournaments that are ongoing, or that start within this window.
LOOKAHEAD = datetime.timedelta(days=7)
# melee.gg leaves some abandoned events un-"Ended" forever. A tournament that
# started longer ago than this is not treated as ongoing, so the first scan
# of a roster doesn't dig up every such leftover.
ONGOING_MAX_AGE = datetime.timedelta(days=30)

Fetch = Callable[[str], Awaitable[list[PlayerTournament]]]


@dataclass(frozen=True)
class Suggestion:
    tournament: PlayerTournament
    players: list[str]   # this server's roster players entered in it


def is_candidate(tournament: PlayerTournament, now: datetime.datetime) -> bool:
    """Ongoing, or starting within LOOKAHEAD."""
    if tournament.ended or tournament.start_time is None:
        return False
    return now - ONGOING_MAX_AGE <= tournament.start_time <= now + LOOKAHEAD


async def scan(usernames: Iterable[str], fetch: Fetch) -> dict[str, list[PlayerTournament]]:
    """Fetch each distinct username's tournaments once — a player on several
    servers' rosters costs one request, not one per server. A username whose
    fetch fails is left out and picked up again by the next scan."""
    results: dict[str, list[PlayerTournament]] = {}
    for username in sorted(set(usernames)):
        try:
            results[username] = await fetch(username)
        except Exception as e:
            log.warning("Discovery: could not fetch tournaments for %s: %s", username, e)
    return results


def suggestions_for(
    roster: Iterable[str],
    results: dict[str, list[PlayerTournament]],
    already_known: set[int],
    now: datetime.datetime,
) -> list[Suggestion]:
    """One suggestion per candidate tournament that any of this server's
    players is entered in, soonest first. `already_known` is every tournament
    the server has bound or has been suggested before."""
    tournaments: dict[int, PlayerTournament] = {}
    players: dict[int, list[str]] = {}
    for player in sorted(set(roster)):
        for t in results.get(player, []):
            if t.tournament_id in already_known or not is_candidate(t, now):
                continue
            tournaments[t.tournament_id] = t
            players.setdefault(t.tournament_id, []).append(player)
    ordered = sorted(tournaments.values(), key=lambda t: (t.start_time, t.tournament_id))
    return [Suggestion(t, players[t.tournament_id]) for t in ordered]


def build_suggestion_embed(
    suggestion: Suggestion, display_names: dict[str, str], now: datetime.datetime,
) -> discord.Embed:
    t = suggestion.tournament
    embed = discord.Embed(
        title=f"🔎 {t.name}"[:256],
        url=f"https://melee.gg/Tournament/View/{t.tournament_id}",
        color=discord.Color.teal(),
    )
    ts = int(t.start_time.timestamp())
    if t.start_time <= now:
        embed.description = f"In progress — started <t:{ts}:R>"
    else:
        embed.description = f"Starts <t:{ts}:F> (<t:{ts}:R>)"
    names = ", ".join(display_names.get(p, p) for p in suggestion.players)
    embed.add_field(name="Your players", value=names[:1024], inline=False)
    embed.set_footer(text="Bind & watch tracks these players here · Ignore dismisses this")
    return embed
