import datetime
import json
import pathlib
from unittest.mock import AsyncMock, patch

import pytest

from melee_tiebreakers.melee_client import (
    PlayerTournament,
    _post_all,
    get_matches,
    get_player_tournaments,
    get_rounds,
    get_tournament_name,
    get_tournament_page,
    parse_player_tournaments,
    parse_tournament_id,
    parse_tournament_page,
)

_FIXTURES = pathlib.Path(__file__).parent / "fixtures"
_MC_GET = "melee_tiebreakers.melee_client._get"

# ── parse_tournament_id ────────────────────────────────────────────────────────

def test_parse_numeric_id():
    assert parse_tournament_id("430189") == 430189

def test_parse_full_url():
    assert parse_tournament_id("https://melee.gg/Tournament/View/430189") == 430189

def test_parse_url_trailing_slash():
    assert parse_tournament_id("https://melee.gg/Tournament/View/430189/") == 430189

def test_parse_with_leading_whitespace():
    assert parse_tournament_id("  430189  ") == 430189

def test_parse_invalid_raises():
    with pytest.raises(ValueError):
        parse_tournament_id("not-an-id")

def test_parse_empty_raises():
    with pytest.raises(ValueError):
        parse_tournament_id("")


# ── pagination (_post_all / get_matches) ───────────────────────────────────────
# melee.gg's DataTables endpoints cap each page at 500 rows regardless of the
# requested `length`. A round/standings page with more rows than that silently
# drops whichever entries sort past the cap unless the client pages through.

def _page(rows: list[int], total: int) -> dict:
    return {"data": [{"id": i} for i in rows], "recordsTotal": total}


async def test_post_all_single_page_when_under_cap():
    post_mock = AsyncMock(return_value=_page([1, 2, 3], total=3))
    with patch("melee_tiebreakers.melee_client._post", post_mock):
        rows = await _post_all("http://x", [("Rank", True)], headers={})
    assert [r["id"] for r in rows] == [1, 2, 3]
    assert post_mock.call_count == 1


async def test_post_all_follows_pagination_past_server_cap():
    post_mock = AsyncMock(side_effect=[
        _page([1, 2], total=5),
        _page([3, 4], total=5),
        _page([5], total=5),
    ])
    with patch("melee_tiebreakers.melee_client._post", post_mock):
        rows = await _post_all("http://x", [("Rank", True)], headers={})
    assert [r["id"] for r in rows] == [1, 2, 3, 4, 5]
    assert post_mock.call_count == 3
    starts = [call.kwargs["data"]["start"] for call in post_mock.call_args_list]
    assert starts == ["0", "2", "4"]


async def test_post_all_stops_on_empty_page():
    post_mock = AsyncMock(side_effect=[_page([1], total=99), _page([], total=99)])
    with patch("melee_tiebreakers.melee_client._post", post_mock):
        rows = await _post_all("http://x", [("Rank", True)], headers={})
    assert [r["id"] for r in rows] == [1]
    assert post_mock.call_count == 2


async def test_get_matches_paginates_when_round_exceeds_cap():
    post_mock = AsyncMock(side_effect=[
        _page([1, 2], total=3),
        _page([3], total=3),
    ])
    with patch("melee_tiebreakers.melee_client._post", post_mock):
        matches = await get_matches(round_id=1, tournament_id=1)
    assert [m["id"] for m in matches] == [1, 2, 3]


# ── tournament page (rounds, name, start time, status from one load) ───────────

@pytest.fixture(scope="module")
def view_html() -> str:
    return (_FIXTURES / "tournament_view.html").read_text()


def test_page_name(view_html):
    assert parse_tournament_page(view_html).name == "2026 Galactic Championship Last Chance Qualifier"


def test_page_start_time_is_utc(view_html):
    assert parse_tournament_page(view_html).start_time == datetime.datetime(
        2026, 7, 24, 16, 0, tzinfo=datetime.UTC
    )


def test_page_status_ended(view_html):
    page = parse_tournament_page(view_html)
    assert page.status == "Ended"
    assert page.ended is True


def test_page_rounds(view_html):
    rounds = parse_tournament_page(view_html).rounds
    assert rounds[0] == {"id": 1419150, "name": "Round 1", "is_started": True}
    assert [r["name"] for r in rounds[:8]] == [f"Round {i}" for i in range(1, 9)]
    assert all(r["is_started"] for r in rounds)


async def test_get_tournament_page_loads_the_page_once(view_html):
    get_mock = AsyncMock(return_value=view_html)
    with patch(_MC_GET, get_mock):
        page = await get_tournament_page(403891)
    assert get_mock.call_count == 1
    assert get_mock.call_args.args[0].endswith("/Tournament/View/403891")
    assert page.rounds and page.name and page.start_time and page.status


def _headline(status_html: str) -> str:
    return f"""
    <html><head><meta content="Some Open" property="og:title"></head><body>
      <p id="tournament-headline-registration">
        <i class="fa fa-user-check mr-1"></i>Open Registration |
        Format: Premier |
        {status_html}
        <i class="fa fa-user mr-1"></i>12 of 64 Enrolled Players
      </p>
    </body></html>
    """


@pytest.mark.parametrize("status", ["In Progress", "Registration Open", "Upcoming", "ended", "Ended Early"])
def test_any_status_other_than_ended_is_not_ended(status):
    page = parse_tournament_page(_headline(f'<i class="fa fa-info-circle mr-1"></i>{status} |'))
    assert page.status == status
    assert page.ended is False


def test_missing_status_is_not_ended():
    page = parse_tournament_page(_headline(""))
    assert page.status is None
    assert page.ended is False


def test_page_without_headline_or_rounds():
    page = parse_tournament_page("<html><head><title>Some Open | Melee</title></head><body></body></html>")
    assert page.name == "Some Open"
    assert page.rounds == []
    assert page.start_time is None
    assert page.status is None


async def test_get_rounds_raises_when_page_has_no_rounds():
    with patch(_MC_GET, AsyncMock(return_value="<html><body></body></html>")):
        with pytest.raises(RuntimeError, match="Round selector not found"):
            await get_rounds(1)


async def test_get_tournament_name_is_best_effort():
    with patch(_MC_GET, AsyncMock(side_effect=Exception("boom"))):
        assert await get_tournament_name(1) is None


# ── a player's tournaments (/Profile/GetResults/{username}) ───────────────────

_MC_POST = "melee_tiebreakers.melee_client._post"


@pytest.fixture(scope="module")
def profile_results() -> dict:
    return json.loads((_FIXTURES / "profile_results.json").read_text())


def test_parse_player_tournaments_from_live_capture(profile_results):
    tournaments = parse_player_tournaments(profile_results["data"])
    assert tournaments[0] == PlayerTournament(
        tournament_id=349338,
        name="Legends of the Force Showdown",
        start_time=datetime.datetime(2025, 8, 19, 23, 0, tzinfo=datetime.UTC),
        status="Ended",
    )
    assert [t.tournament_id for t in tournaments] == [349338, 349838, 376062]
    assert all(t.ended for t in tournaments)
    assert all(t.start_time.tzinfo is not None for t in tournaments)


def test_parse_player_tournaments_other_statuses_are_not_ended():
    rows = [
        {"TournamentId": 1, "TournamentName": "Upcoming Open", "TournamentStartDate": "2026-10-03T09:00:00Z",
         "TournamentStatusDescription": "Registration Open"},
        {"TournamentId": 2, "TournamentName": None, "TournamentStartDate": None,
         "TournamentStatusDescription": None},
    ]
    upcoming, bare = parse_player_tournaments(rows)
    assert upcoming.status == "Registration Open" and upcoming.ended is False
    assert bare == PlayerTournament(2, "Tournament 2", None, None)
    assert bare.ended is False


async def test_get_player_tournaments_posts_to_the_profile_endpoint(profile_results):
    # The capture is 3 of 70 rows, so the client asks for the next page; end it there.
    post_mock = AsyncMock(side_effect=[profile_results, {"data": [], "recordsTotal": 70}])
    with patch(_MC_POST, post_mock):
        tournaments = await get_player_tournaments("IvoryTiger71")
    assert len(tournaments) == 3
    first, second = post_mock.call_args_list
    assert first.args[0] == "https://melee.gg/Profile/GetResults/IvoryTiger71"
    assert first.kwargs["headers"]["Referer"] == "https://melee.gg/Profile/Index/IvoryTiger71"
    assert second.kwargs["data"]["start"] == "3"


async def test_get_player_tournaments_escapes_the_username():
    post_mock = AsyncMock(return_value={"data": [], "recordsTotal": 0})
    with patch(_MC_POST, post_mock):
        assert await get_player_tournaments("Some Name/x") == []
    assert post_mock.call_args.args[0] == "https://melee.gg/Profile/GetResults/Some%20Name%2Fx"
