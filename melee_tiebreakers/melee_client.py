"""Minimal client for melee.gg's tournament data endpoints.

Extracted from melee-bot's tracker.py: session setup, request throttling,
and the DataTables-backed endpoints (rounds, matches, standings) that feed
tiebreaker calculations, plus a player's tournament list from their profile.
No bot- or Discord-specific logic lives here.
"""

import asyncio
import datetime
import logging
import os
import re
import time
from dataclasses import dataclass, field
from functools import partial
from urllib.parse import quote

from bs4 import BeautifulSoup
from curl_cffi.requests import Session

log = logging.getLogger(__name__)

# melee.gg's robots.txt specifies "Crawl-Delay: 5" — enforced globally across
# every outbound request so bursts (e.g. one GetRoundMatches call per round)
# can't slip under it.
_MELEE_GG = "https://melee.gg"
# MELEE_BASE_URL points the client at a stand-in for the real site (the fake
# in demo_fake_melee/). The crawl delay is melee.gg's rule, so it applies to
# melee.gg only.
BASE_URL = os.getenv("MELEE_BASE_URL", _MELEE_GG).rstrip("/")
_CRAWL_DELAY_SECONDS = 5.0 if BASE_URL == _MELEE_GG else 0.0
_rate_lock = asyncio.Lock()
_last_request_at = 0.0

_session = Session(impersonate="chrome120")
_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:151.0) Gecko/20100101 Firefox/151.0",
    "Accept-Language": "en-GB,en;q=0.9",
    "Origin": BASE_URL,
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
})
_session.cookies.update({
    "FunctionalCookie": "true",
    "AnalyticalCookie": "true",
    "CookieConsent": "true",
})


def parse_tournament_id(s: str) -> int:
    s = s.strip().rstrip("/")
    m = re.search(r"(\d+)$", s)
    if m:
        return int(m.group(1))
    raise ValueError(f"Cannot parse tournament ID from: {s!r}")


def _ajax_headers(tournament_id: int) -> dict:
    return {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{BASE_URL}/Tournament/View/{tournament_id}",
    }


def _dt_body(
    columns: list[tuple[str, bool]],
    extra: dict | None = None,
    length: int = 1000,
) -> dict:
    body: dict = {
        "draw": "1", "start": "0", "length": str(length),
        "order[0][column]": "0", "order[0][dir]": "asc",
        "search[value]": "", "search[regex]": "false",
    }
    for i, (name, searchable) in enumerate(columns):
        s = "true" if searchable else "false"
        body.update({
            f"columns[{i}][data]": name,
            f"columns[{i}][name]": name,
            f"columns[{i}][searchable]": s,
            f"columns[{i}][orderable]": s,
            f"columns[{i}][search][value]": "",
            f"columns[{i}][search][regex]": "false",
        })
    if extra:
        body.update(extra)
    return body


async def _throttle() -> None:
    global _last_request_at
    async with _rate_lock:
        wait = _last_request_at + _CRAWL_DELAY_SECONDS - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_at = time.monotonic()


async def _get(url: str) -> str:
    await _throttle()
    log.debug("GET %s", url)
    loop = asyncio.get_event_loop()
    resp = await loop.run_in_executor(None, partial(_session.get, url, timeout=15))
    log.debug("GET %s → %s", url, resp.status_code)
    resp.raise_for_status()
    return resp.text


async def _post(url: str, data: dict, headers: dict) -> dict:
    await _throttle()
    log.debug("POST %s", url)
    loop = asyncio.get_event_loop()
    resp = await loop.run_in_executor(
        None, partial(_session.post, url, data=data, headers=headers, timeout=15)
    )
    log.debug("POST %s → %s", url, resp.status_code)
    resp.raise_for_status()
    return resp.json()


async def _post_all(
    url: str,
    columns: list[tuple[str, bool]],
    headers: dict,
    extra: dict | None = None,
) -> list[dict]:
    """POST a DataTables endpoint, following pagination until every row is fetched.

    melee.gg caps `GetRoundMatches`/`GetRoundStandings` at 500 rows per page
    regardless of the requested `length` — a round or standings page with more
    entries than that silently truncates whichever rows sort past the cap.
    """
    rows: list[dict] = []
    while True:
        body = _dt_body(columns, extra=extra)
        body["start"] = str(len(rows))
        data = await _post(url, data=body, headers=headers)
        page = data.get("data", [])
        if not page:
            break
        rows.extend(page)
        total = data.get("recordsTotal", len(rows))
        if len(rows) >= total:
            break
    return rows


# The only status value observed on a finished tournament's page so far.
# Anything else — including a status we fail to parse — counts as not ended.
STATUS_ENDED = "Ended"


@dataclass(frozen=True)
class TournamentPage:
    """Everything the bot reads from one load of /Tournament/View/{id}."""
    rounds: list[dict] = field(default_factory=list)   # [{"id", "name", "is_started"}], in page order
    name: str | None = None
    start_time: datetime.datetime | None = None        # timezone-aware, UTC
    status: str | None = None                          # as shown on the page, e.g. "Ended"

    @property
    def ended(self) -> bool:
        return self.status == STATUS_ENDED


def _parse_start_time(soup: BeautifulSoup) -> datetime.datetime | None:
    holder = soup.find(id="tournament-headline-start-date-field")
    span = holder.find(attrs={"data-value": True}) if holder else None
    if not span:
        return None
    # e.g. "2026-07-24T16:00:00.0000000Z" — .NET's seven fractional digits.
    raw = re.sub(r"\.\d+", "", span["data-value"].strip())
    try:
        parsed = datetime.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed.astimezone(datetime.UTC)


def _parse_status(soup: BeautifulSoup) -> str | None:
    # The headline's registration line is a "|"-separated run of icon + text
    # pairs; the status is the text after the info-circle icon.
    line = soup.find(id="tournament-headline-registration")
    icon = line.find("i", class_="fa-info-circle") if line else None
    if not icon or not isinstance(icon.next_sibling, str):
        return None
    return icon.next_sibling.split("|")[0].strip() or None


def parse_tournament_page(html: str) -> TournamentPage:
    soup = BeautifulSoup(html, "html.parser")

    name = None
    meta = soup.find("meta", property="og:title")
    if meta and meta.get("content"):
        name = meta["content"].strip()
    elif soup.title and soup.title.string:
        name = soup.title.string.split(" | ")[0].strip()

    rounds = []
    container = soup.find(id="pairings-round-selector-container")
    for btn in container.find_all("button") if container else []:
        rounds.append({
            "id": int(btn["data-id"]),
            "name": btn.get("data-name", f"Round {btn['data-id']}"),
            "is_started": btn.get("data-is-started", "false").lower() == "true",
        })

    return TournamentPage(
        rounds=rounds, name=name, start_time=_parse_start_time(soup), status=_parse_status(soup),
    )


async def get_tournament_page(tournament_id: int) -> TournamentPage:
    """Rounds, name, start time and status from a single page load."""
    return parse_tournament_page(await _get(f"{BASE_URL}/Tournament/View/{tournament_id}"))


async def get_tournament_name(tournament_id: int) -> str | None:
    """Best-effort scrape of the tournament's display name from its melee.gg page."""
    try:
        return (await get_tournament_page(tournament_id)).name
    except Exception as e:
        log.warning("get_tournament_name %s failed: %s", tournament_id, e)
        return None


async def get_rounds(tournament_id: int) -> list[dict]:
    page = await get_tournament_page(tournament_id)
    if not page.rounds:
        raise RuntimeError(f"Round selector not found for {tournament_id} — may not have started.")
    return page.rounds


async def get_matches(round_id: int, tournament_id: int) -> list[dict]:
    columns = [
        ("TableNumber", True), ("PodNumber", True),
        ("Teams", False), ("Decklists", False), ("ResultString", False),
    ]
    return await _post_all(
        f"{BASE_URL}/Match/GetRoundMatches/{round_id}",
        columns,
        headers=_ajax_headers(tournament_id),
    )


async def get_standings(round_id: int, tournament_id: int) -> list[dict]:
    """Raw standings rows for a round, straight from melee.gg's GetRoundStandings.

    Each row includes melee.gg's own tiebreaker fields (OpponentMatchWinPercentage,
    TeamGameWinPercentage, OpponentGameWinPercentage, FinalTiebreaker) alongside
    rank/record/points. Use `standings.parse_standings` to turn these into
    typed `Standing` objects.
    """
    columns = [
        ("Rank", True), ("Player", False), ("Decklists", False),
        ("MatchRecord", False), ("GameRecord", False), ("Points", True),
        ("OpponentMatchWinPercentage", False), ("TeamGameWinPercentage", False),
        ("OpponentGameWinPercentage", False), ("FinalTiebreaker", False),
        ("OpponentCount", True),
    ]
    return await _post_all(
        f"{BASE_URL}/Standing/GetRoundStandings",
        columns,
        headers=_ajax_headers(tournament_id),
        extra={"roundId": str(round_id)},
    )


@dataclass(frozen=True)
class PlayerTournament:
    """One tournament a player is, or was, entered in."""
    tournament_id: int
    name: str
    start_time: datetime.datetime | None   # timezone-aware, UTC
    status: str | None                     # e.g. "Ended"; same wording as the tournament page

    @property
    def ended(self) -> bool:
        return self.status == STATUS_ENDED


def _parse_utc(raw: str | None) -> datetime.datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed.astimezone(datetime.UTC)


def parse_player_tournaments(rows: list[dict]) -> list[PlayerTournament]:
    return [
        PlayerTournament(
            tournament_id=int(row["TournamentId"]),
            name=(row.get("TournamentName") or f"Tournament {row['TournamentId']}").strip(),
            start_time=_parse_utc(row.get("TournamentStartDate")),
            status=row.get("TournamentStatusDescription") or None,
        )
        for row in rows
        if row.get("TournamentId")
    ]


async def get_player_tournaments(username: str) -> list[PlayerTournament]:
    """Every tournament on a player's melee.gg profile, oldest first.

    `username` must be the exact melee.gg username — this is the profile's
    own results table, not a search. An unknown username yields an empty list.
    """
    columns = [
        ("TournamentStartDate", True), ("TournamentName", True), ("Game", True),
        ("DecklistId", False), ("Rank", True), ("Record", False), ("Format", True),
    ]
    user = quote(username, safe="")
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{BASE_URL}/Profile/Index/{user}",
    }
    rows = await _post_all(f"{BASE_URL}/Profile/GetResults/{user}", columns, headers=headers)
    return parse_player_tournaments(rows)
