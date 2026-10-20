import datetime
import logging
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

import discord

from melee_tiebreakers.melee_client import (
    STATUS_ENDED,
    get_matches,
    get_standings,
    get_tournament_name,  # noqa: F401 — re-exported for commands/*.py
    get_tournament_page,
    parse_tournament_id,  # noqa: F401 — re-exported for commands/*.py
)

log = logging.getLogger(__name__)

_FINAL_ROUND_NAMES = frozenset({"finals", "final", "grand finals", "grand final"})


def _is_final_round(r: dict) -> bool:
    return r["name"].lower().strip() in _FINAL_ROUND_NAMES


def _comp_player_names(competitor: dict) -> list[str]:
    """Return all identifying names from a single competitor/team entry.

    Includes both Username and DisplayName for each player so that ResultStrings
    that use the display name (e.g. "Yael Millbrook won 2-1-0" when the username
    is "MistyComet") can still be matched correctly.
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


def _player_names(match: dict) -> tuple[str, str]:
    comps = match.get("Competitors", [])
    p1 = _comp_player_names(comps[0])[0] if len(comps) > 0 else "?"
    p2 = _comp_player_names(comps[1])[0] if len(comps) > 1 else "BYE"
    return p1, p2


def _find_match(matches: list[dict], player: str) -> dict | None:
    needle = player.lower()
    for m in matches:
        for comp in m.get("Competitors", []):
            if any(needle in n.lower() for n in _comp_player_names(comp)):
                return m
    return None


def _find_standing(standings: list[dict], player: str) -> dict | None:
    needle = player.lower()
    for s in standings:
        for p in s.get("Team", {}).get("Players", []):
            if needle in (p.get("Username") or "").lower() \
                    or needle in (p.get("DisplayName") or "").lower():
                return s
    return None


@dataclass
class PlayerSnapshot:
    rounds: dict[str, str] = field(default_factory=dict)     # round_name -> result
    opponents: dict[str, str] = field(default_factory=dict)  # round_name -> opponent
    rank: int | None = None
    record: str | None = None
    points: int | None = None


@dataclass
class TournamentSnapshot:
    tournament_id: int
    players: dict[str, PlayerSnapshot] = field(default_factory=dict)
    error: str | None = None
    last_round_is_final: bool = False
    rounds_started: int = 0
    # Facts about the event, read from the same page load as the rounds.
    # None when the page could not be loaded (or did not show them).
    name: str | None = None
    start_time: datetime.datetime | None = None
    status: str | None = None
    # The page loaded fine but no round has started: `error` holds the
    # message to show, yet this is a state to wait out, not a failed fetch.
    not_started: bool = False

    @property
    def ended(self) -> bool:
        """True once melee.gg itself shows the tournament as ended."""
        return self.status == STATUS_ENDED

    def is_complete(self, rounds_to_track: int | None = None) -> bool:
        """True when every tracked player has a result in every tracked round.

        If rounds_to_track is given, tracking concludes once that many rounds
        have started and all results for them are in — this is the preferred
        path since it doesn't depend on melee.gg's naming of the final round.
        Otherwise falls back to detecting the last started round by name
        (Finals/Grand Finals/etc, see _is_final_round).
        """
        if not self.players:
            return False
        if rounds_to_track is not None:
            if self.rounds_started < rounds_to_track:
                return False
        elif not self.last_round_is_final:
            return False
        all_rounds = [ps.rounds for ps in self.players.values()]
        if not any(all_rounds):
            return False
        return all(
            result != "Not reported"
            for ps in self.players.values()
            for result in ps.rounds.values()
        )


@dataclass
class Change:
    player: str
    description: str
    positive: bool | None = None  # True=good, False=bad, None=neutral


async def fetch_snapshot(
    tournament_id: int,
    players: list[str],
    cached_rounds: dict[str, dict[str, tuple[str, str]]] | None = None,
) -> TournamentSnapshot:
    """Fetch a live snapshot for the given players.

    cached_rounds: {player: {round_name: (result, opponent)}} for rounds whose
    results are already finalised.  Any round where every tracked player has a
    cached entry is skipped — no API call is made for it.
    """
    snap = TournamentSnapshot(tournament_id=tournament_id)
    cached = cached_rounds or {}

    log.info("fetch_snapshot tournament=%s players=%s", tournament_id, players)

    try:
        page = await get_tournament_page(tournament_id)
    except Exception as e:
        log.error("fetch_snapshot tournament=%s failed to load its page: %s", tournament_id, e)
        snap.error = str(e)
        return snap
    rounds = page.rounds
    snap.name, snap.start_time, snap.status = page.name, page.start_time, page.status

    started = [r for r in rounds if r["is_started"]]
    log.info("tournament=%s rounds total=%s started=%s", tournament_id, len(rounds), len(started))
    snap.last_round_is_final = bool(started) and _is_final_round(started[-1])
    snap.rounds_started = len(started)

    if not started:
        snap.error = "No rounds have started yet."
        snap.not_started = True
        return snap

    round_matches: dict[str, list[dict]] = {}
    for rnd in started:
        round_name = rnd["name"]
        if all(round_name in cached.get(p, {}) for p in players):
            log.debug("tournament=%s round=%s skipped (cached)", tournament_id, round_name)
            continue
        try:
            round_matches[round_name] = await get_matches(rnd["id"], tournament_id)
        except Exception as e:
            log.error("tournament=%s failed to get matches for %s: %s", tournament_id, rnd["name"], e)

    standings: list[dict] = []
    try:
        standings = await get_standings(started[-1]["id"], tournament_id)
    except Exception as e:
        log.error("tournament=%s failed to get standings: %s", tournament_id, e)

    for player in players:
        ps = PlayerSnapshot()
        for rnd in started:
            round_name = rnd["name"]
            if round_name not in round_matches:
                # Use cached data for this round
                cached_entry = cached.get(player, {}).get(round_name)
                if cached_entry:
                    ps.rounds[round_name], ps.opponents[round_name] = cached_entry
                continue
            m = _find_match(round_matches[round_name], player)
            if m:
                comps = m.get("Competitors", [])
                opp_names: list[str] = []
                for i, comp in enumerate(comps):
                    names = _comp_player_names(comp)
                    if any(player.lower() in n.lower() for n in names):
                        # opponent is every other competitor
                        opp_names = [
                            n for j, c in enumerate(comps) if j != i
                            for n in _comp_player_names(c)
                        ]
                        break
                ps.opponents[round_name] = " / ".join(opp_names) if opp_names else "?"
                ps.rounds[round_name] = m.get("ResultString") or "Not reported"
        s = _find_standing(standings, player)
        if s:
            ps.rank = s.get("Rank")
            ps.record = s.get("MatchRecord")
            ps.points = s.get("Points")
        log.info(
            "tournament=%s player=%s rounds=%s rank=%s",
            tournament_id, player, list(ps.rounds.keys()), ps.rank,
        )
        snap.players[player] = ps

    return snap


def diff(old: TournamentSnapshot, new: TournamentSnapshot) -> list[Change]:
    changes: list[Change] = []

    for player, new_ps in new.players.items():
        old_ps = old.players.get(player, PlayerSnapshot())

        for round_name in set(new_ps.rounds) - set(old_ps.rounds):
            if new_ps.rounds[round_name] == "Not reported":
                opponent = new_ps.opponents.get(round_name, "?")
                changes.append(Change(player, f"{round_name} paired vs {opponent}"))

        for round_name, result in new_ps.rounds.items():
            if old_ps.rounds.get(round_name, "Not reported") == "Not reported" \
                    and result != "Not reported":
                opponent = new_ps.opponents.get(round_name, "?")
                positive: bool | None = None
                lower = result.lower()
                opponent = new_ps.opponents.get(round_name, "")
                if "bye" in lower:
                    positive = True
                elif "draw" in lower:
                    positive = None
                elif " won " in lower:
                    winner = result[:lower.index(" won ")].strip()
                    opp_names = [o.strip() for o in opponent.split(" / ") if o.strip()]
                    positive = not (opp_names and any(o.lower() in winner.lower() for o in opp_names))
                else:
                    parts = result.split("-")
                    if len(parts) >= 2:
                        try:
                            positive = int(parts[0]) > int(parts[1])
                        except ValueError:
                            pass
                changes.append(Change(player, f"{round_name} vs {opponent}: **{result}**", positive))

        if (
            old_ps.rank is not None
            and new_ps.rank is not None
            and old_ps.rank != new_ps.rank
        ):
            arrow = "▲" if new_ps.rank < old_ps.rank else "▼"
            changes.append(Change(
                player,
                f"Rank {old_ps.rank} → {new_ps.rank} {arrow}",
                new_ps.rank < old_ps.rank,
            ))

    return changes


# ── /watch session model + embed building (moved from bot.py) ─────────────────

LONDON = ZoneInfo("Europe/London")
_RESULT_EMOJI = {"win": "✅", "loss": "❌", "draw": "🤝", "pending": "⏳"}


def _count_results(ps: PlayerSnapshot) -> tuple[int, int, int]:
    """Returns (wins, losses, draws) from completed rounds only."""
    wins = losses = draws = 0
    for round_name, result in ps.rounds.items():
        if result == "Not reported":
            continue
        lower = result.lower()
        if "bye" in lower:
            wins += 1
        elif "draw" in lower:
            draws += 1
        elif " won " in lower:
            opponent = ps.opponents.get(round_name, "")
            opp_names = [o.strip() for o in opponent.split(" / ") if o.strip()]
            winner = result[:lower.index(" won ")].strip()
            if opp_names and any(o.lower() in winner.lower() for o in opp_names):
                losses += 1
            else:
                wins += 1
        else:
            parts = result.split("-")
            if len(parts) >= 2:
                try:
                    w, opp_score = int(parts[0]), int(parts[1])
                    if w > opp_score:
                        wins += 1
                    elif opp_score > w:
                        losses += 1
                    else:
                        draws += 1
                except ValueError:
                    pass
    return wins, losses, draws


def _short_round(name: str) -> str:
    if name.lower().startswith("round "):
        return "R" + name[6:]
    return name


def _result_emoji(result: str, opponent: str = "") -> str:
    if result == "Not reported":
        return _RESULT_EMOJI["pending"]
    lower = result.lower()
    if "bye" in lower:
        return _RESULT_EMOJI["win"]
    if "draw" in lower:
        return _RESULT_EMOJI["draw"]
    if " won " in lower:
        winner = result[:lower.index(" won ")].strip()
        opp_names = [o.strip() for o in opponent.split(" / ") if o.strip()]
        if opp_names and any(o.lower() in winner.lower() for o in opp_names):
            return _RESULT_EMOJI["loss"]
        elif opp_names:
            return _RESULT_EMOJI["win"]
        return ""
    parts = result.split("-")
    if len(parts) >= 2:
        try:
            w, opp_score = int(parts[0]), int(parts[1])
            if w > opp_score:
                return _RESULT_EMOJI["win"]
            if opp_score > w:
                return _RESULT_EMOJI["loss"]
            return _RESULT_EMOJI["draw"]
        except ValueError:
            pass
    return ""


def tally_str(ps: PlayerSnapshot, include_pending: bool = False) -> str:
    wins, losses, draws = _count_results(ps)
    parts = [f"{wins}W · {losses}L"]
    if draws:
        parts.append(f"{draws}D")
    if include_pending:
        pending = sum(1 for r in ps.rounds.values() if r == "Not reported")
        if pending:
            parts.append(f"{pending} pending")
    return " · ".join(parts)


def format_players(players: list[str], display_names: dict[str, str]) -> str:
    return ", ".join(display_names.get(p, p) for p in players)


@dataclass
class TournamentWatch:
    tournament_id: int
    players: list[str]
    snapshot: TournamentSnapshot
    display_names: dict[str, str] = field(default_factory=dict)
    rounds_to_track: int | None = None
    name: str | None = None
    # Why this tournament stopped being polled for this watch (see
    # domain.feed.CONCLUDED_*); None while it is still live.
    concluded: str | None = None

    @property
    def title(self) -> str:
        return self.name or f"Tournament {self.tournament_id}"


@dataclass
class WatchSession:
    guild_id: int
    channel_id: int
    message_id: int
    tournament_watches: list[TournamentWatch]
    # Every live tournament in the watch is being polled at the idle rate.
    idle: bool = False
    # When this watch last had something new to show (this process only).
    last_change_at: datetime.datetime | None = None


def _not_started_text(snap: TournamentSnapshot) -> str:
    if snap.start_time:
        return f"⏳ Hasn't started yet · starts <t:{int(snap.start_time.timestamp())}:R>"
    return "⏳ Hasn't started yet"


def build_embed(
    session: WatchSession,
    changes: list[Change],
    concluded: bool = False,
) -> discord.Embed:
    color = (
        discord.Color.greyple() if concluded
        else discord.Color.gold() if changes
        else discord.Color.blue()
    )
    embed = discord.Embed(title="🏆 melee.gg Live Tracker", color=color)

    all_display_names: dict[str, str] = {}
    for tw in session.tournament_watches:
        all_display_names.update(tw.display_names)

    for tw in session.tournament_watches:
        if tw.snapshot.not_started:
            field_value = _not_started_text(tw.snapshot)
        elif tw.snapshot.error:
            field_value = f"⚠️ {tw.snapshot.error}"
        else:
            lines: list[str] = []
            max_rounds = max((len(ps.rounds) for ps in tw.snapshot.players.values()), default=0)
            for player, ps in tw.snapshot.players.items():
                display = all_display_names.get(player, player)
                tally = tally_str(ps)
                dropped = max_rounds >= 2 and len(ps.rounds) <= max_rounds - 2
                standing_parts = list(filter(None, [
                    f"Rank {ps.rank}" if ps.rank else None,
                    f"{ps.points}pts" if ps.points is not None else None,
                    "dropped" if dropped else None,
                ]))
                header = f"**{display}** — {tally}"
                if standing_parts:
                    header += f"  ·  {' · '.join(standing_parts)}"
                round_lines = []
                for rn, result in ps.rounds.items():
                    opponent = ps.opponents.get(rn, "?")
                    emoji = _result_emoji(result, opponent)
                    round_lines.append(f"{_short_round(rn)} {emoji} {opponent}")
                lines.append(header + "\n" + "\n".join(round_lines) if round_lines else header)
            field_value = "\n\n".join(lines) or "No players found in pairings."
        embed.add_field(
            name=f"[{tw.title}](https://melee.gg/Tournament/View/{tw.tournament_id})"
                 + (" · 🏁 concluded" if tw.concluded and not concluded else ""),
            value=field_value,
            inline=False,
        )

    if changes:
        change_lines = []
        for c in changes:
            display = all_display_names.get(c.player, c.player)
            emoji = "🟢" if c.positive else ("🔴" if c.positive is False else "⚪")
            change_lines.append(f"{emoji} **{display}** — {c.description}")
        embed.add_field(name="🔔 Changes", value="\n".join(change_lines), inline=False)

    now = datetime.datetime.now(LONDON).strftime("%H:%M %Z")
    if concluded:
        embed.set_footer(text=f"Tournament concluded · {now}")
    elif session.idle:
        embed.set_footer(text=f"💤 Waiting for the next round · checked {now} · /watch stop to end")
    else:
        embed.set_footer(text=f"Updated {now} · /watch stop to end")

    return embed
