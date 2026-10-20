"""Fetch/domain orchestration for /topcut — mirrors tracker.py's role for
/watch: turns live melee.gg data into a resolved result, no Discord code.

Round-position detection (are we in the last or second-to-last Swiss round?)
assumes melee.gg's round selector already lists every Swiss round by name
("Round N") up front, even ones not yet started, so the highest N gives the
total Swiss round count. This matches the SWU Comprehensive Event Guide's
description of the single-elimination phase being created before Swiss
pairs its first round, but hasn't been independently confirmed against a
genuinely in-progress tournament (see docs/research-pairing-and-tiebreakers.md's
open questions) — if it turns out future rounds aren't listed in advance,
this will misclassify an early round as "the last round". Treat this as the
first thing to verify against a live tournament.

Enumeration scope: the projected final round (second-to-last-round case)
pairs the WHOLE field correctly (pairing.pair_final_round needs full
standings to avoid the narrow-window pairing bug found during development —
see tests/test_two_round_projection.py), but only OUTSTANDING matches
involving a "relevant" player (tracked roster players, or anyone within
`_RELEVANCE_MARGIN` ranks of the cut line) are branched — everyone else's
match is real but irrelevant to whether a tracked player makes the cut, and
branching it too would blow the exact-enumeration cap for no benefit.
"""

import datetime
import logging
import re
from dataclasses import dataclass, field

import discord

from domain import db as database
from melee_tiebreakers import melee_client
from melee_tiebreakers.enumeration import (
    MatchOutcome,
    OutstandingMatch,
    RoundContext,
    TooManyBranchesError,
    TournamentState,
)
from melee_tiebreakers.match_history import RematchGraph, build_rematch_graph, parse_round_matches
from melee_tiebreakers.standings import Standing, find_standing, parse_standings
from melee_tiebreakers.summarize import PlayerCutResult, cut_results_for_roster
from melee_tiebreakers.tiebreakers import TeamRecord, apply_match_result, record_from_strings

log = logging.getLogger(__name__)

_SWISS_ROUND_RE = re.compile(r"^round\s+(\d+)$", re.IGNORECASE)
_RELEVANCE_MARGIN = 8   # players within cut_size + this many ranks are "in contention"
_BUBBLE_THRESHOLD = 5   # outstanding relevant matches at/below this -> aggressive polling
NORMAL_CADENCE_SECONDS = 60
BUBBLE_CADENCE_SECONDS = 15
_EMPTY_RECORD = TeamRecord(0, 0, 0, 0, 0, 0)


class RoundPositionError(Exception):
    """Tournament isn't currently in its last or second-to-last Swiss round,
    or hasn't got a completed prior round to use as a baseline yet."""


def _swiss_round_number(name: str) -> int | None:
    m = _SWISS_ROUND_RE.match(name.strip())
    return int(m.group(1)) if m else None


def classify_round_position(rounds: list[dict]) -> tuple[dict, bool]:
    """Returns (current_round, project_next_round)."""
    swiss_rounds = [(r, n) for r in rounds if (n := _swiss_round_number(r["name"])) is not None]
    if not swiss_rounds:
        raise RoundPositionError("Could not find any Swiss rounds for this tournament.")
    max_swiss_number = max(n for _, n in swiss_rounds)
    started_swiss = [(r, n) for r, n in swiss_rounds if r["is_started"]]
    if not started_swiss:
        raise RoundPositionError("Swiss hasn't started yet.")
    current_round, current_number = max(started_swiss, key=lambda pair: pair[1])

    if current_number == max_swiss_number:
        return current_round, False
    if current_number == max_swiss_number - 1:
        return current_round, True
    raise RoundPositionError(
        f"/topcut only works during the last or second-to-last Swiss round "
        f"(currently Round {current_number} of {max_swiss_number})."
    )


def _relevant_team_ids(standings: list[Standing], cut_size: int, tracked: set[int]) -> set[int]:
    ordered = sorted(standings, key=lambda s: s.rank)
    band = {s.team_id for s in ordered[: cut_size + _RELEVANCE_MARGIN]}
    return band | tracked


async def _get_matches_cached(tournament_id: int, round_id: int, cache: dict[int, list[dict]]) -> list[dict]:
    if round_id in cache:
        return cache[round_id]
    rows = await melee_client.get_matches(round_id, tournament_id)
    await database.save_round_match_cache(tournament_id, round_id, rows)
    return rows


@dataclass
class FetchedData:
    current_round: dict
    project_next_round: bool
    baseline_standings: list[Standing]
    current_matches: list   # list[MatchResult]
    rematch_graph: RematchGraph


async def fetch_tournament_data(tournament_id: int) -> FetchedData:
    """One round-trip's worth of fetching, reused for both resolving roster
    names to team IDs and building the TournamentState — avoids fetching the
    same standings/matches twice."""
    rounds = await melee_client.get_rounds(tournament_id)
    current_round, project_next_round = classify_round_position(rounds)
    current_number = _swiss_round_number(current_round["name"])

    swiss_rounds = sorted(
        ((r, n) for r in rounds if (n := _swiss_round_number(r["name"])) is not None),
        key=lambda pair: pair[1],
    )
    prior_rounds = [(r, n) for r, n in swiss_rounds if n < current_number]
    if not prior_rounds:
        raise RoundPositionError("No completed Swiss round to use as a baseline yet.")
    baseline_round = prior_rounds[-1][0]

    cached = await database.load_round_match_cache(tournament_id)
    completed_matches = []
    for r, _ in prior_rounds:
        rows = await _get_matches_cached(tournament_id, r["id"], cached)
        completed_matches.append(parse_round_matches(rows))
    rematch_graph = build_rematch_graph(completed_matches)

    baseline_rows = await melee_client.get_standings(baseline_round["id"], tournament_id)
    baseline_standings = parse_standings(baseline_rows)
    current_matches_raw = await melee_client.get_matches(current_round["id"], tournament_id)
    current_matches = parse_round_matches(current_matches_raw)

    return FetchedData(current_round, project_next_round, baseline_standings, current_matches, rematch_graph)


def build_state(data: FetchedData, cut_size: int, tracked_team_ids: set[int]) -> TournamentState:
    baseline_records = {
        s.team_id: record_from_strings(s.match_record, s.game_record)
        for s in data.baseline_standings if s.team_id is not None
    }
    relevant = _relevant_team_ids(data.baseline_standings, cut_size, tracked_team_ids)

    round_pairing = []
    outstanding = []
    for m in data.current_matches:
        if len(m.competitor_team_ids) != 2:
            continue
        a, b = m.competitor_team_ids
        round_pairing.append((a, b))
        if m.reported:
            a_gw, b_gw = m.game_wins.get(a, 0), m.game_wins.get(b, 0)
            a_rec = baseline_records.get(a, _EMPTY_RECORD)
            b_rec = baseline_records.get(b, _EMPTY_RECORD)
            baseline_records[a] = apply_match_result(a_rec, a_gw, b_gw, m.game_draws)
            baseline_records[b] = apply_match_result(b_rec, b_gw, a_gw, m.game_draws)
        elif a in relevant or b in relevant:
            outstanding.append(OutstandingMatch(team_a=a, team_b=b, table_number=m.table_number))

    ctx = RoundContext(
        baseline_records=baseline_records,
        opponents_before=dict(data.rematch_graph.played),
        round_pairing=round_pairing,
        outstanding=outstanding,
        fixed_opponent_records=baseline_records,
    )
    return TournamentState(
        current_round=ctx,
        rematch_graph_before_current=data.rematch_graph,
        project_next_round=data.project_next_round,
    )


def is_bubble(state: TournamentState) -> bool:
    return len(state.current_round.outstanding) <= _BUBBLE_THRESHOLD


@dataclass
class TopCutSession:
    guild_id: int
    channel_id: int
    message_id: int
    tournament_id: int
    cut_size: int
    started_by: str
    tournament_name: str | None = None
    results: dict[str, PlayerCutResult] = field(default_factory=dict)   # keyed by roster player name
    error: str | None = None
    bubble: bool = False
    concluded: bool = False
    next_attempt: datetime.datetime | None = None   # normal/bubble cadence backoff


async def refresh_session(session: TopCutSession) -> None:
    """Fetch fresh state and recompute every roster player's cut result."""
    now = datetime.datetime.now(datetime.UTC)

    players = await database.list_players(session.guild_id)
    try:
        data = await fetch_tournament_data(session.tournament_id)
    except RoundPositionError as e:
        session.error = str(e)
        session.next_attempt = now + datetime.timedelta(seconds=NORMAL_CADENCE_SECONDS)
        return

    team_id_by_player: dict[str, int] = {}
    for name in players:
        s = find_standing(data.baseline_standings, name)
        if s and s.team_id is not None:
            team_id_by_player[name] = s.team_id
    if not team_id_by_player:
        session.error = "None of the roster players were found in this tournament's standings."
        session.next_attempt = now + datetime.timedelta(seconds=NORMAL_CADENCE_SECONDS)
        return

    tracked_team_ids = set(team_id_by_player.values())
    state = build_state(data, session.cut_size, tracked_team_ids)

    try:
        results = cut_results_for_roster(state, list(tracked_team_ids), session.cut_size)
    except TooManyBranchesError:
        session.error = (
            "Too many outstanding matches to enumerate exactly yet — will retry as results come in."
        )
        session.next_attempt = now + datetime.timedelta(seconds=NORMAL_CADENCE_SECONDS)
        return

    session.error = None
    session.results = {
        name: results[team_id] for name, team_id in team_id_by_player.items() if team_id in results
    }
    session.bubble = is_bubble(state)
    session.concluded = not state.current_round.outstanding and not state.project_next_round
    cadence = BUBBLE_CADENCE_SECONDS if session.bubble else NORMAL_CADENCE_SECONDS
    session.next_attempt = now + datetime.timedelta(seconds=cadence)


_OUTCOME_LABEL = {MatchOutcome.A_WINS: "WIN", MatchOutcome.B_WINS: "LOSE", MatchOutcome.DRAW: "DRAW"}


def build_topcut_embed(session: TopCutSession, display_names: dict[str, str]) -> discord.Embed:
    color = (
        discord.Color.greyple() if session.concluded
        else discord.Color.gold() if session.bubble
        else discord.Color.blue()
    )
    title_url = f"https://melee.gg/Tournament/View/{session.tournament_id}"
    title = session.tournament_name or f"Tournament {session.tournament_id}"
    embed = discord.Embed(title=f"🏆 Top {session.cut_size} Watch — [{title}]({title_url})", color=color)

    if session.error:
        embed.description = f"⚠️ {session.error}"
        return embed

    for name, result in session.results.items():
        display = display_names.get(name, name)
        if result.clinched:
            embed.add_field(name=display, value="✓ Clinched", inline=False)
            continue
        if result.eliminated:
            embed.add_field(name=display, value="✗ Eliminated", inline=False)
            continue

        lines = []
        for b in result.breakdowns:
            label = _OUTCOME_LABEL.get(b.outcome, "no live match this round")
            mark = "✓" if b.made_cut == b.total else ("✗" if b.made_cut == 0 else "⚠")
            line = f"{mark} If you {label}: makes it in {b.made_cut}/{b.total}"
            table_names = [f"Table {m.table_number}" for m in b.swinging_matches if m.table_number]
            if table_names:
                line += f" — depends on: {', '.join(table_names)}"
            if b.game_score_sensitive:
                line += " (game-score sensitive)"
            lines.append(line)
        embed.add_field(name=display, value="\n".join(lines), inline=False)

    if session.concluded:
        embed.set_footer(text="Concluded")
    elif session.bubble:
        embed.set_footer(text="Bubble mode — polling aggressively")
    else:
        embed.set_footer(text="/topcut stop to end")

    return embed
