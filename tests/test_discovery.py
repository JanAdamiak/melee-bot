"""Discovery: which tournaments to suggest to which server, and the scan that feeds it."""

import asyncio
import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import pytest

import domain.db as db
from commands.discovery import BindSuggestionButton, IgnoreSuggestionButton, suggestion_view
from commands.views import MAX_TOURNAMENTS
from domain import discovery
from domain.discovery import LOOKAHEAD, ONGOING_MAX_AGE, Suggestion, is_candidate, scan, suggestions_for
from domain.feed import Activity
from domain.tracker import TournamentSnapshot
from melee_tiebreakers.melee_client import PlayerTournament
from scheduler import PRIORITY_DISCOVERY, PRIORITY_TOPCUT_NORMAL, PRIORITY_WATCH, FetchScheduler

NOW = datetime.datetime(2026, 10, 1, 12, 0, tzinfo=datetime.UTC)
GUILD_A, GUILD_B = 1, 2


def _days(n: float) -> datetime.timedelta:
    return datetime.timedelta(days=n)


def _t(
    tid: int, start: datetime.datetime | None, status: str | None = "Registration Open",
) -> PlayerTournament:
    return PlayerTournament(tid, f"Tournament {tid}", start, status)


UPCOMING = _t(1, NOW + _days(2))
ONGOING = _t(2, NOW - _days(1), "In Progress")
FAR_OFF = _t(3, NOW + _days(30))
FINISHED = _t(4, NOW - _days(1), "Ended")


# ── what counts as worth suggesting ────────────────────────────────────────────

def test_upcoming_within_a_week_is_a_candidate():
    assert is_candidate(UPCOMING, NOW)
    assert is_candidate(_t(9, NOW + LOOKAHEAD), NOW)


def test_starting_later_than_a_week_is_not():
    assert not is_candidate(_t(9, NOW + LOOKAHEAD + _days(0.01)), NOW)
    assert not is_candidate(FAR_OFF, NOW)


def test_ongoing_is_a_candidate():
    assert is_candidate(ONGOING, NOW)


def test_ended_is_never_a_candidate():
    assert not is_candidate(FINISHED, NOW)
    assert not is_candidate(_t(9, NOW + _days(1), "Ended"), NOW)


def test_unknown_status_counts_as_not_ended():
    assert is_candidate(_t(9, NOW + _days(1), None), NOW)


def test_long_abandoned_unended_tournament_is_not_ongoing():
    assert not is_candidate(_t(9, NOW - ONGOING_MAX_AGE - _days(1), "In Progress"), NOW)


def test_tournament_without_a_start_time_is_skipped():
    assert not is_candidate(_t(9, None), NOW)


# ── the scan ───────────────────────────────────────────────────────────────────

async def test_scan_fetches_a_shared_player_once():
    fetch = AsyncMock(return_value=[UPCOMING])
    rosters = {GUILD_A: ["Alice", "Bob"], GUILD_B: ["Bob", "Cara"]}
    results = await scan((p for roster in rosters.values() for p in roster), fetch)
    assert sorted(c.args[0] for c in fetch.call_args_list) == ["Alice", "Bob", "Cara"]
    assert set(results) == {"Alice", "Bob", "Cara"}


async def test_scan_skips_a_player_whose_fetch_fails():
    async def fetch(username):
        if username == "Bob":
            raise RuntimeError("melee.gg down")
        return [UPCOMING]
    results = await scan(["Alice", "Bob"], fetch)
    assert results == {"Alice": [UPCOMING]}


def test_discovery_is_the_lowest_priority():
    assert PRIORITY_DISCOVERY == 20
    assert PRIORITY_DISCOVERY > PRIORITY_WATCH > PRIORITY_TOPCUT_NORMAL


async def test_discovery_fetch_waits_behind_a_queued_watch_fetch():
    scheduler = FetchScheduler()
    order = []

    def job(name):
        async def fn():
            order.append(name)
        return fn

    first = asyncio.ensure_future(scheduler.submit(PRIORITY_DISCOVERY, job("discovery")))
    await asyncio.sleep(0)
    second = asyncio.ensure_future(scheduler.submit(PRIORITY_WATCH, job("watch")))
    await asyncio.sleep(0)
    await scheduler.start()
    await asyncio.gather(first, second)
    await scheduler.stop()
    assert order == ["watch", "discovery"]


# ── per-server suggestions ─────────────────────────────────────────────────────

RESULTS = {
    "Alice": [UPCOMING, FINISHED],
    "Bob": [UPCOMING, ONGOING, FAR_OFF],
    "Cara": [ONGOING],
}


def test_one_suggestion_per_tournament_listing_that_servers_players():
    assert suggestions_for(["Alice", "Bob"], RESULTS, set(), NOW) == [
        Suggestion(ONGOING, ["Bob"]),
        Suggestion(UPCOMING, ["Alice", "Bob"]),
    ]


def test_each_server_sees_only_its_own_players():
    assert suggestions_for(["Bob", "Cara"], RESULTS, set(), NOW) == [
        Suggestion(ONGOING, ["Bob", "Cara"]),
        Suggestion(UPCOMING, ["Bob"]),
    ]


def test_bound_or_already_suggested_tournaments_are_left_out():
    already = {UPCOMING.tournament_id}
    assert suggestions_for(["Alice", "Bob"], RESULTS, already, NOW) == [Suggestion(ONGOING, ["Bob"])]


def test_roster_player_with_no_results_is_fine():
    assert suggestions_for(["Nobody"], RESULTS, set(), NOW) == []


# ── the message ────────────────────────────────────────────────────────────────

def test_suggestion_embed_for_an_upcoming_tournament():
    embed = discovery.build_suggestion_embed(Suggestion(UPCOMING, ["Alice", "Bob"]), {"Alice": "Ali"}, NOW)
    assert "Tournament 1" in embed.title
    assert embed.url == "https://melee.gg/Tournament/View/1"
    assert embed.description.startswith("Starts <t:")
    assert embed.fields[0].value == "Ali, Bob"


def test_suggestion_embed_for_an_ongoing_tournament():
    embed = discovery.build_suggestion_embed(Suggestion(ONGOING, ["Bob"]), {}, NOW)
    assert embed.description.startswith("In progress")


async def test_buttons_carry_the_tournament_id_so_they_survive_a_restart():
    view = suggestion_view(430189)
    assert view.timeout is None
    bind, ignore = view.children
    assert bind.custom_id == "discovery:bind:430189"
    assert ignore.custom_id == "discovery:ignore:430189"

    # What discord.py does with a click after a restart: match the custom_id
    # against the registered template and rebuild the item from it.
    for cls, item in ((BindSuggestionButton, bind), (IgnoreSuggestionButton, ignore)):
        match = cls.__discord_ui_compiled_template__.fullmatch(item.custom_id)
        rebuilt = await cls.from_custom_id(None, item, match)
        assert rebuilt.tournament_id == 430189


# ── the bot's scan task, end to end (fake Discord, real DB and scheduler) ──────

class _Channel:
    """The few TextChannel methods the bot uses, recording what was sent."""

    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.sent: list[dict] = []
        self._next_message_id = channel_id * 1000

    async def send(self, **kwargs):
        self._next_message_id += 1
        self.sent.append(kwargs)
        return SimpleNamespace(id=self._next_message_id)

    async def fetch_message(self, message_id: int):
        return SimpleNamespace(id=message_id, embeds=[], edit=AsyncMock())


@pytest.fixture
async def bot():
    import bot as bot_module

    await db.init_db()
    b = bot_module.MeleeBot()
    await b.scheduler.start()
    channels = {555: _Channel(555), 666: _Channel(666)}
    with patch.object(b, "get_channel", side_effect=channels.get):
        b.channels = channels
        yield b
    await b.scheduler.stop()


async def _two_servers() -> None:
    for guild, channel, roster in ((GUILD_A, 555, ["Alice", "Bob"]), (GUILD_B, 666, ["Bob", "Cara"])):
        await db.set_discovery_channel(guild, channel)
        for player in roster:
            await db.add_player(guild, player, "u")


def _patch_profiles(results: dict[str, list[PlayerTournament]]):
    async def fetch(username):
        return results.get(username, [])
    return patch("bot.get_player_tournaments", AsyncMock(side_effect=fetch))


def _patch_now():
    real = datetime.datetime

    class _Fixed(real):
        @classmethod
        def now(cls, tz=None):
            return NOW
    return patch("bot.datetime.datetime", _Fixed)


async def test_scan_posts_one_message_per_tournament_to_each_server(bot):
    await _two_servers()
    with _patch_profiles(RESULTS) as fetch, _patch_now():
        await bot._run_discovery()

    assert fetch.call_count == 3  # Alice, Bob, Cara — Bob once despite two rosters
    a, b = bot.channels[555].sent, bot.channels[666].sent
    assert [m["embed"].fields[0].value for m in a] == ["Bob", "Alice, Bob"]
    assert [m["embed"].fields[0].value for m in b] == ["Bob, Cara", "Bob"]
    assert [c.custom_id for c in a[0]["view"].children] == ["discovery:bind:2", "discovery:ignore:2"]

    stored = await db.get_discovery_suggestion(GUILD_A, UPCOMING.tournament_id)
    assert stored["status"] == db.SUGGESTION_PENDING
    assert stored["players"] == ["Alice", "Bob"]
    assert stored["message_id"] is not None


async def test_second_scan_does_not_repeat_suggestions(bot):
    await _two_servers()
    with _patch_profiles(RESULTS), _patch_now():
        await bot._run_discovery()
        await db.set_discovery_suggestion_status(GUILD_A, ONGOING.tournament_id, db.SUGGESTION_IGNORED)
        await bot._run_discovery()
    assert len(bot.channels[555].sent) == 2
    assert len(bot.channels[666].sent) == 2


async def test_scan_skips_tournaments_the_server_already_bound(bot):
    await _two_servers()
    await db.add_tournament(GUILD_A, UPCOMING.tournament_id, "u")
    with _patch_profiles(RESULTS), _patch_now():
        await bot._run_discovery()
    assert [m["embed"].fields[0].value for m in bot.channels[555].sent] == ["Bob"]  # only ONGOING
    assert len(bot.channels[666].sent) == 2  # the other server is unaffected


async def test_scan_does_nothing_for_servers_with_discovery_off(bot):
    await _two_servers()
    await db.set_discovery_channel(GUILD_B, None)
    with _patch_profiles(RESULTS) as fetch, _patch_now():
        await bot._run_discovery()
    assert sorted(c.args[0] for c in fetch.call_args_list) == ["Alice", "Bob"]  # Cara not fetched
    assert bot.channels[666].sent == []


async def test_failed_post_is_retried_by_the_next_scan(bot):
    await _two_servers()
    await db.set_discovery_channel(GUILD_B, None)
    channel = bot.channels[555]
    boom = discord.HTTPException(SimpleNamespace(status=500, reason="x"), "x")
    with _patch_profiles({"Alice": [UPCOMING]}), _patch_now():
        with patch.object(channel, "send", AsyncMock(side_effect=boom)):
            await bot._run_discovery()
        assert await db.list_suggested_tournament_ids(GUILD_A) == set()
        await bot._run_discovery()
    assert len(channel.sent) == 1


# ── Bind & watch: where the watch goes ─────────────────────────────────────────

def _not_started(tid, players, cached_rounds=None):
    return TournamentSnapshot(tid, error="No rounds have started yet.", not_started=True)


async def _bind(guild: int, tid: int) -> None:
    await db.add_tournament(guild, tid, "u", name=f"Tournament {tid}")
    await db.set_tournament_players(guild, tid, ["Alice"])


async def test_watch_in_channel_starts_a_watch_that_idles_until_round_one(bot):
    await _bind(GUILD_A, 1)
    channel = bot.channels[555]
    with patch("domain.feed.tracker.fetch_snapshot", AsyncMock(side_effect=_not_started)):
        outcome = await bot.watch_in_channel(GUILD_A, channel, 1)

    assert outcome == "Started a watch in <#555>."
    session = bot.watches[555]
    assert session.guild_id == GUILD_A and session.idle is True
    assert bot.feeds.feeds[1].activity is Activity.IDLE
    assert "Hasn't started yet" in channel.sent[0]["embed"].fields[0].value
    assert await db.load_watch_sessions() == [(GUILD_A, 555, session.message_id, [1])]


async def test_watch_in_channel_adds_to_the_existing_watch_up_to_the_limit(bot):
    channel = bot.channels[555]
    with patch("domain.feed.tracker.fetch_snapshot", AsyncMock(side_effect=_not_started)):
        for tid in range(1, MAX_TOURNAMENTS + 1):
            await _bind(GUILD_A, tid)
            outcome = await bot.watch_in_channel(GUILD_A, channel, tid)
        assert outcome == "Added it to the watch in <#555>."
        assert len(channel.sent) == 1  # one embed, edited as tournaments are added
        session = bot.watches[555]
        assert [tw.tournament_id for tw in session.tournament_watches] == [1, 2, 3, 4]
        assert (await db.load_watch_sessions())[0][3] == [1, 2, 3, 4]

        await _bind(GUILD_A, 5)
        outcome = await bot.watch_in_channel(GUILD_A, channel, 5)
    assert "already has 4 tournaments" in outcome
    assert len(session.tournament_watches) == MAX_TOURNAMENTS
    assert 5 not in bot.feeds.feeds  # not subscribed, so never polled


async def test_watch_in_channel_does_not_duplicate_a_tournament(bot):
    await _bind(GUILD_A, 1)
    channel = bot.channels[555]
    with patch("domain.feed.tracker.fetch_snapshot", AsyncMock(side_effect=_not_started)):
        await bot.watch_in_channel(GUILD_A, channel, 1)
        outcome = await bot.watch_in_channel(GUILD_A, channel, 1)
    assert outcome == "It is already being watched in <#555>."
    assert len(bot.watches[555].tournament_watches) == 1
