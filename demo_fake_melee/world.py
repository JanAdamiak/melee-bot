"""The fake site's data: fixture tournaments, renamed and replayed step by step.

A tournament's position is a single `step`. Step 0 is "not started"; after
that every Swiss round takes three steps — paired, half its results in, all
its results in — so advancing one step always gives the bot something new.
"""

import copy
import datetime
import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from demo_fake_melee import scenario
from demo_fake_melee.names import build_aliases
from melee_tiebreakers.melee_client import STATUS_ENDED
from melee_tiebreakers.tiebreakers import (
    ProjectedStanding,
    TeamRecord,
    apply_match_result,
    ogw_pct,
    omw_pct,
    rank_all,
)

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
SWISS_ROUNDS = 12
PHASES = ("paired", "half", "done")
LAST_STEP = SWISS_ROUNDS * len(PHASES)

# Only "Ended" has been seen on the real site (see tests/fixtures/README.md);
# these two are invented. The bot treats anything but "Ended" as not ended.
STATUS_UPCOMING = "Registration Open"
STATUS_RUNNING = "In Progress"

_DRAW_RE = re.compile(r"^\d+-\d+-\d+ Draw$")
_EMPTY_RECORD = TeamRecord(0, 0, 0, 0, 0, 0)


def step_for(round_number: int, phase: str = "paired") -> int:
    """The step at which `round_number` is in `phase`; round 0 is "not started"."""
    if not 0 <= round_number <= SWISS_ROUNDS:
        raise ValueError(f"round must be 0-{SWISS_ROUNDS}, got {round_number}")
    if round_number == 0:
        return 0
    return (round_number - 1) * len(PHASES) + PHASES.index(phase) + 1


def _load_rounds(fixture: int) -> list[list[dict]]:
    folder = FIXTURES / f"tournament_{fixture}"
    return [
        json.loads((folder / f"matches_round_{n:02d}.json").read_text())
        for n in range(1, SWISS_ROUNDS + 1)
    ]


def _players(rounds: list[list[dict]]):
    for rows in rounds:
        for row in rows:
            for comp in row["Competitors"]:
                yield comp["TeamId"], comp["Team"]["Players"]


def _is_reported(row: dict) -> bool:
    return bool(row.get("HasResult"))


def compute_standings(rounds: list[list[dict]]) -> list[dict]:
    """GetRoundStandings-shaped rows for everyone in `rounds`, counting only
    the matches that have a result."""
    teams: dict[int, dict] = {}
    records: dict[int, TeamRecord] = {}
    opponents: dict[int, list[int]] = {}
    for rows in rounds:
        for row in rows:
            comps = row["Competitors"]
            for comp in comps:
                teams.setdefault(comp["TeamId"], comp["Team"])
                records.setdefault(comp["TeamId"], _EMPTY_RECORD)
                opponents.setdefault(comp["TeamId"], [])
            if not _is_reported(row):
                continue
            if len(comps) == 1:
                tid = comps[0]["TeamId"]
                records[tid] = apply_match_result(records[tid], comps[0].get("GameByes") or 2, 0)
            elif len(comps) == 2:
                a, b = comps
                a_wins, b_wins = a.get("GameWins") or 0, b.get("GameWins") or 0
                draws = row.get("GameDraws") or 0
                records[a["TeamId"]] = apply_match_result(records[a["TeamId"]], a_wins, b_wins, draws)
                records[b["TeamId"]] = apply_match_result(records[b["TeamId"]], b_wins, a_wins, draws)
                opponents[a["TeamId"]].append(b["TeamId"])
                opponents[b["TeamId"]].append(a["TeamId"])

    projected = [
        ProjectedStanding(
            team_id=tid,
            points=rec.match_wins * 3 + rec.match_draws,
            omw_pct=omw_pct([records[o].match_pct() for o in opponents[tid]]),
            tgw_pct=rec.game_pct(),
            ogw_pct=ogw_pct([records[o].game_pct() for o in opponents[tid]]),
        )
        for tid, rec in records.items()
    ]
    rows = []
    for rank, s in enumerate(rank_all(projected), start=1):
        rec = records[s.team_id]
        rows.append({
            "Rank": rank,
            "TeamId": s.team_id,
            "Team": teams[s.team_id],
            "Points": s.points,
            "MatchRecord": f"{rec.match_wins}-{rec.match_losses}-{rec.match_draws}",
            "GameRecord": f"{rec.game_wins}-{rec.game_losses}-{rec.game_draws}",
            "OpponentMatchWinPercentage": round(s.omw_pct, 8),
            "TeamGameWinPercentage": round(s.tgw_pct, 8),
            "OpponentGameWinPercentage": round(s.ogw_pct, 8),
            "FinalTiebreaker": float(s.team_id),
            "OpponentCount": len(opponents[s.team_id]),
        })
    return rows


def _renamed_result(row: dict, alias_by_team: dict[int, str]) -> str | None:
    """The row's ResultString with the real name swapped for its alias.
    Raises on a wording it doesn't know, rather than let a real name through."""
    result = row.get("ResultString")
    if not result or _DRAW_RE.match(result):
        return result
    for comp in row["Competitors"]:
        for player in comp["Team"]["Players"]:
            for real in (player.get("DisplayName"), player.get("Username")):
                if real and result.startswith(real + " "):
                    return alias_by_team[comp["TeamId"]] + result[len(real):]
    raise ValueError(f"Unrecognised ResultString: {result!r}")


def _blank(row: dict) -> dict:
    """The row as it looks once paired but before its result is reported."""
    blank = dict(row, HasResult=False, ResultString=None, GameDraws=0)
    blank["Competitors"] = [dict(comp, GameWins=None) for comp in row["Competitors"]]
    return blank


@dataclass
class Tournament:
    id: int
    name: str
    start_time: datetime.datetime
    rounds: list[list[dict]]            # renamed match rows, one list per Swiss round
    usernames: set[str]
    step: int = 0
    ended: bool = False
    initial: tuple = field(default=(), repr=False)   # (step, start_time) to reset to

    def round_id(self, number: int) -> int:
        return self.id * 100 + number

    @property
    def current_round(self) -> int:
        return (self.step + len(PHASES) - 1) // len(PHASES)

    @property
    def phase(self) -> str | None:
        return PHASES[(self.step - 1) % len(PHASES)] if self.step else None

    @property
    def status(self) -> str:
        if self.ended:
            return STATUS_ENDED
        return STATUS_RUNNING if self.step else STATUS_UPCOMING

    @property
    def position(self) -> str:
        if not self.step:
            return "Not started"
        wording = {"paired": "paired, no results", "half": "half the results in", "done": "all results in"}
        return f"Round {self.current_round}: {wording[self.phase]}"

    def matches(self, number: int) -> list[dict]:
        """Round `number` as the site would show it right now."""
        if not 1 <= number <= self.current_round:
            return []
        rows = self.rounds[number - 1]
        if number < self.current_round or self.phase == "done":
            return rows
        # Byes are known as soon as the round is paired.
        return [
            row if len(row["Competitors"]) == 1 or (self.phase == "half" and i % 2 == 0) else _blank(row)
            for i, row in enumerate(rows)
        ]

    def standings(self, number: int) -> list[dict]:
        """Standings as of round `number`, live while that round is being played."""
        if not 1 <= number <= self.current_round:
            return []
        return compute_standings([self.matches(n) for n in range(1, number + 1)])


def _fixture_names() -> set[str]:
    """Every username and display name in the scenario's fixtures."""
    return {p[key] for spec in scenario.TOURNAMENTS for _, players in _players(_load_rounds(spec["fixture"]))
            for p in players for key in ("Username", "DisplayName")}


def _build_tournament(spec: dict, now: datetime.datetime, fixture_names: set[str]) -> Tournament:
    raw_rounds = _load_rounds(spec["fixture"])
    username_by_team = {tid: players[0]["Username"] for tid, players in _players(raw_rounds)}

    # Cast the demo players by where the real ones finished.
    final = compute_standings(raw_rounds)
    cast = {final[rank - 1]["TeamId"]: name for name, rank in spec["cast"].items()}
    # The fixtures' names are invented too, but drawn from the same pool, so keep
    # them out of the aliases or a renamed player could take another's fixture name.
    generic = build_aliases(username_by_team.values(), reserved=set(spec["cast"]) | fixture_names)
    alias_by_team = {tid: cast.get(tid) or generic[real] for tid, real in username_by_team.items()}

    tournament = Tournament(
        id=spec["id"],
        name=spec["name"],
        start_time=(now + datetime.timedelta(hours=spec["starts_in_hours"])).replace(microsecond=0),
        rounds=[],
        usernames=set(alias_by_team.values()),
        step=step_for(spec["round"], spec["phase"]),
    )
    for number, rows in enumerate(raw_rounds, start=1):
        renamed = []
        for row in rows:
            new = copy.deepcopy(row)
            new["RoundId"] = tournament.round_id(number)
            new["ResultString"] = _renamed_result(row, alias_by_team)
            for comp in new["Competitors"]:
                alias = alias_by_team[comp["TeamId"]]
                comp["Team"]["Players"] = [{"Username": alias, "DisplayName": alias}]
            renamed.append(new)
        tournament.rounds.append(renamed)
    tournament.initial = (tournament.step, tournament.start_time)
    return tournament


class World:
    """Every demo tournament plus the controls that move them along.

    With `state_path`, positions survive a restart of the fake server — the
    bot caches finished rounds, so the fake must not jump back behind it.
    """

    def __init__(self, state_path: Path | None = None, now: datetime.datetime | None = None) -> None:
        now = now or datetime.datetime.now(datetime.UTC)
        self.lock = threading.Lock()
        self.state_path = state_path
        fixture_names = _fixture_names()
        self.tournaments = {
            spec["id"]: _build_tournament(spec, now, fixture_names) for spec in scenario.TOURNAMENTS
        }
        self._load_state()

    # ── lookups ───────────────────────────────────────────────────────────────

    def round(self, round_id: int) -> tuple[Tournament, int] | None:
        tournament = self.tournaments.get(round_id // 100)
        number = round_id % 100
        if tournament is None or not 1 <= number <= SWISS_ROUNDS:
            return None
        return tournament, number

    def profile(self, username: str) -> list[dict]:
        """GetResults-shaped rows: the tournaments `username` is entered in, oldest first."""
        entered = [t for t in self.tournaments.values() if username in t.usernames]
        return [
            {
                "UserName": username,
                "TournamentId": t.id,
                "TournamentName": t.name,
                "TournamentStartDate": t.start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "TournamentStatusDescription": t.status,
            }
            for t in sorted(entered, key=lambda t: t.start_time)
        ]

    # ── controls ──────────────────────────────────────────────────────────────

    def advance(self, tournament_id: int) -> None:
        """One step forward; past the last round's results, the tournament ends."""
        t = self.tournaments[tournament_id]
        if t.step >= LAST_STEP:
            t.ended = True
        else:
            self._move(t, t.step + 1)
        self._save_state()

    def goto(self, tournament_id: int, round_number: int, phase: str = "paired") -> None:
        t = self.tournaments[tournament_id]
        t.ended = False
        self._move(t, step_for(round_number, phase))
        self._save_state()

    def end(self, tournament_id: int) -> None:
        self.tournaments[tournament_id].ended = True
        self._save_state()

    def reset(self) -> None:
        """Everything back to how scenario.py describes it."""
        for t in self.tournaments.values():
            t.step, t.start_time = t.initial
            t.ended = False
        self._save_state()

    def _move(self, t: Tournament, step: int) -> None:
        t.step = step
        now = datetime.datetime.now(datetime.UTC).replace(microsecond=0)
        if step and t.start_time > now:
            t.start_time = now   # round 1 can't be paired before the start time

    # ── persistence ───────────────────────────────────────────────────────────

    def _load_state(self) -> None:
        if not self.state_path or not self.state_path.exists():
            return
        saved = json.loads(self.state_path.read_text())
        for t in self.tournaments.values():
            entry = saved.get(str(t.id))
            if entry:
                t.step = entry["step"]
                t.ended = entry["ended"]
                t.start_time = datetime.datetime.fromisoformat(entry["start_time"])

    def _save_state(self) -> None:
        if not self.state_path:
            return
        self.state_path.write_text(json.dumps({
            str(t.id): {"step": t.step, "ended": t.ended, "start_time": t.start_time.isoformat()}
            for t in self.tournaments.values()
        }, indent=2))
