"""TournamentFeed / FeedManager: one fetch per tournament, shared by every watch."""

import datetime
from unittest.mock import AsyncMock, patch

import pytest

import domain.db as db
from domain.feed import (
    ACTIVE_POLL,
    CONCLUDED_ENDED,
    CONCLUDED_STALE,
    FRESH_EMBED_AFTER,
    IDLE_AFTER,
    IDLE_POLL,
    REUSE_SNAPSHOT_SECONDS,
    STALE_AFTER,
    Activity,
    FeedManager,
)
from domain.tracker import PlayerSnapshot, TournamentSnapshot, TournamentWatch, WatchSession
from scheduler import FetchScheduler

T0 = datetime.datetime(2026, 10, 1, 12, 0, tzinfo=datetime.UTC)
TID = 430189
# Two watches in different servers; the watch key is the channel ID.
CHANNEL_A, PLAYERS_A = 111, ["Alice", "Bob"]
CHANNEL_B, PLAYERS_B = 222, ["Bob", "Cara"]


def _minutes(n: float) -> datetime.timedelta:
    return datetime.timedelta(minutes=n)


def _snapshot(tournament_id: int, players: list[str], result: str = "2-0") -> TournamentSnapshot:
    return TournamentSnapshot(
        tournament_id=tournament_id,
        players={
            p: PlayerSnapshot(rounds={"Round 1": result}, opponents={"Round 1": "Zed"}, rank=i + 1)
            for i, p in enumerate(players)
        },
        rounds_started=1,
    )


def _fake_fetch(result: str = "2-0") -> AsyncMock:
    """Stands in for tracker.fetch_snapshot: answers for whoever was asked about."""
    async def fetch(tournament_id, players, cached_rounds=None):
        return _snapshot(tournament_id, players, result)
    return AsyncMock(side_effect=fetch)


@pytest.fixture(autouse=True)
async def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    await db.init_db()


@pytest.fixture
async def feeds():
    scheduler = FetchScheduler()
    await scheduler.start()
    yield FeedManager(scheduler)
    await scheduler.stop()


def _patch_fetch(mock: AsyncMock):
    return patch("domain.feed.tracker.fetch_snapshot", mock)


# ── one fetch per tournament ───────────────────────────────────────────────────

async def test_two_watches_on_one_tournament_produce_one_fetch(feeds):
    feeds.subscribe(CHANNEL_A, TID, PLAYERS_A)
    feeds.subscribe(CHANNEL_B, TID, PLAYERS_B)
    fetch = _fake_fetch()
    with _patch_fetch(fetch):
        refreshed = await feeds.poll(T0)

    assert refreshed == {TID}
    assert fetch.call_count == 1
    tid, players, _cache = fetch.call_args.args
    assert tid == TID
    assert players == ["Alice", "Bob", "Cara"]  # combined, shared player once


async def test_each_watch_sees_only_its_own_players(feeds):
    feeds.subscribe(CHANNEL_A, TID, PLAYERS_A)
    feeds.subscribe(CHANNEL_B, TID, PLAYERS_B)
    with _patch_fetch(_fake_fetch()):
        await feeds.poll(T0)

    feed = feeds.feeds[TID]
    assert set(feed.view(PLAYERS_A).players) == {"Alice", "Bob"}
    assert set(feed.view(PLAYERS_B).players) == {"Bob", "Cara"}
    assert feed.view(PLAYERS_A).rounds_started == 1


async def test_different_tournaments_are_fetched_separately(feeds):
    feeds.subscribe(CHANNEL_A, TID, PLAYERS_A)
    feeds.subscribe(CHANNEL_A, TID + 1, PLAYERS_A)
    fetch = _fake_fetch()
    with _patch_fetch(fetch):
        refreshed = await feeds.poll(T0)
    assert refreshed == {TID, TID + 1}
    assert fetch.call_count == 2


async def test_poll_saves_finalized_rounds_and_passes_them_back_as_cache(feeds):
    feeds.subscribe(CHANNEL_A, TID, ["Alice"])
    fetch = _fake_fetch()
    with _patch_fetch(fetch):
        await feeds.poll(T0)
        await feeds.poll(T0 + _minutes(1))
    assert await db.load_round_results(TID) == {"Alice": {"Round 1": ("2-0", "Zed")}}
    assert fetch.call_args_list[0].args[2] == {}
    assert fetch.call_args_list[1].args[2] == {"Alice": {"Round 1": ("2-0", "Zed")}}


# ── starting a watch ───────────────────────────────────────────────────────────

async def test_join_fetches_when_there_is_no_feed_yet(feeds):
    fetch = _fake_fetch()
    with _patch_fetch(fetch):
        snap = await feeds.join(CHANNEL_A, TID, PLAYERS_A, now=T0)
    assert fetch.call_count == 1
    assert set(snap.players) == {"Alice", "Bob"}


async def test_join_goes_through_the_scheduler(feeds):
    with _patch_fetch(_fake_fetch()), \
            patch.object(feeds.scheduler, "submit", wraps=feeds.scheduler.submit) as submit:
        await feeds.join(CHANNEL_A, TID, PLAYERS_A, now=T0)
    assert submit.call_count == 1


async def test_join_reuses_a_recent_snapshot_that_covers_its_players(feeds):
    fetch = _fake_fetch()
    with _patch_fetch(fetch):
        await feeds.join(CHANNEL_A, TID, ["Alice", "Bob", "Cara"], now=T0)
        snap = await feeds.join(CHANNEL_B, TID, PLAYERS_B, now=T0 + _minutes(1))
    assert fetch.call_count == 1
    assert set(snap.players) == {"Bob", "Cara"}


async def test_join_refetches_when_its_players_are_not_covered(feeds):
    fetch = _fake_fetch()
    with _patch_fetch(fetch):
        await feeds.join(CHANNEL_A, TID, PLAYERS_A, now=T0)
        snap = await feeds.join(CHANNEL_B, TID, PLAYERS_B, now=T0 + _minutes(1))
    assert fetch.call_count == 2
    assert fetch.call_args.args[1] == ["Alice", "Bob", "Cara"]
    assert set(snap.players) == {"Bob", "Cara"}


async def test_join_refetches_when_the_snapshot_is_old(feeds):
    fetch = _fake_fetch()
    later = T0 + datetime.timedelta(seconds=REUSE_SNAPSHOT_SECONDS + 1)
    with _patch_fetch(fetch):
        await feeds.join(CHANNEL_A, TID, PLAYERS_A, now=T0)
        await feeds.join(CHANNEL_B, TID, PLAYERS_A, now=later)
    assert fetch.call_count == 2


# ── unsubscribing ──────────────────────────────────────────────────────────────

async def test_unsubscribe_narrows_the_combined_player_list(feeds):
    feeds.subscribe(CHANNEL_A, TID, PLAYERS_A)
    feeds.subscribe(CHANNEL_B, TID, PLAYERS_B)
    feeds.unsubscribe(CHANNEL_A)
    assert feeds.feeds[TID].players == ["Bob", "Cara"]


async def test_feed_is_dropped_when_its_last_watch_leaves(feeds):
    feeds.subscribe(CHANNEL_A, TID, PLAYERS_A)
    feeds.subscribe(CHANNEL_A, TID + 1, PLAYERS_A)
    feeds.unsubscribe(CHANNEL_A, TID)
    assert set(feeds.feeds) == {TID + 1}
    feeds.unsubscribe(CHANNEL_A)
    assert feeds.feeds == {}


# ── failures and backoff ───────────────────────────────────────────────────────

async def test_failed_fetch_backs_off_and_keeps_the_last_good_snapshot(feeds):
    feeds.subscribe(CHANNEL_A, TID, PLAYERS_A)
    with _patch_fetch(_fake_fetch()):
        await feeds.poll(T0)
    failing = AsyncMock(return_value=TournamentSnapshot(TID, error="connection refused"))
    with _patch_fetch(failing):
        assert await feeds.poll(T0 + _minutes(1)) == set()
        # still inside the 2-minute backoff: no new attempt
        assert await feeds.poll(T0 + _minutes(2)) == set()
    feed = feeds.feeds[TID]
    assert failing.call_count == 1
    assert feed.failures == 1
    assert feed.next_poll_at == T0 + _minutes(3)
    assert feed.view(PLAYERS_A).error is None
    assert set(feed.view(PLAYERS_A).players) == {"Alice", "Bob"}


async def test_success_after_failure_resets_backoff(feeds):
    feeds.subscribe(CHANNEL_A, TID, PLAYERS_A)
    with _patch_fetch(AsyncMock(return_value=TournamentSnapshot(TID, error="boom"))):
        await feeds.poll(T0)
    assert feeds.feeds[TID].view(PLAYERS_A).error == "boom"  # nothing better to show yet
    with _patch_fetch(_fake_fetch()):
        assert await feeds.poll(T0 + _minutes(5)) == {TID}
    feed = feeds.feeds[TID]
    assert feed.failures == 0 and feed.next_poll_at == T0 + _minutes(6)
    assert feed.view(PLAYERS_A).error is None


# ── bubble back-off ────────────────────────────────────────────────────────────

async def test_bubble_mode_polls_every_third_tick(feeds):
    feeds.subscribe(CHANNEL_A, TID, PLAYERS_A)
    fetch = _fake_fetch()
    with _patch_fetch(fetch):
        results = [await feeds.poll(T0 + _minutes(i), bubble_active=True) for i in range(6)]
    assert [bool(r) for r in results] == [True, False, False, True, False, False]
    assert fetch.call_count == 2


# ── tournament facts ───────────────────────────────────────────────────────────

async def test_poll_stores_start_time_and_status_and_views_carry_them(feeds):
    await db.add_tournament(1, TID, "u")
    feeds.subscribe(CHANNEL_A, TID, PLAYERS_A)

    async def fetch(tournament_id, players, cached_rounds=None):
        snap = _snapshot(tournament_id, players)
        snap.start_time, snap.status = T0, "Ended"
        return snap

    with _patch_fetch(AsyncMock(side_effect=fetch)), \
            patch("domain.feed.database.set_tournament_info", wraps=db.set_tournament_info) as store:
        await feeds.poll(T0)
        await feeds.poll(T0 + _minutes(1))

    assert store.call_count == 1  # unchanged on the second poll: no second write
    info = await db.get_tournament_info(TID)
    assert info["start_time"] == T0 and info["status"] == "Ended"
    assert feeds.feeds[TID].view(PLAYERS_A).ended is True


async def test_status_is_stored_even_when_no_round_has_started(feeds):
    await db.add_tournament(1, TID, "u")
    feeds.subscribe(CHANNEL_A, TID, PLAYERS_A)
    not_started = TournamentSnapshot(
        TID, error="No rounds have started yet.", start_time=T0, status="Upcoming"
    )
    with _patch_fetch(AsyncMock(return_value=not_started)):
        await feeds.poll(T0)
    assert (await db.get_tournament_info(TID))["status"] == "Upcoming"


# ── concluding ─────────────────────────────────────────────────────────────────
# A tournament concludes for a watch when melee.gg shows it as ended, or after
# 5 days with no changes. Nothing else — in particular not "every round reported".

GUILD_A, GUILD_B = 1, 2


def _fetch_with_status(status: str | None, result: str = "2-0") -> AsyncMock:
    async def fetch(tournament_id, players, cached_rounds=None):
        snap = _snapshot(tournament_id, players, result)
        snap.status = status
        return snap
    return AsyncMock(side_effect=fetch)


async def _watch(feeds, guild_id, channel_id, tournaments: dict[int, list[str]], now=T0) -> WatchSession:
    tws = []
    for tid, players in tournaments.items():
        snap = await feeds.join(channel_id, tid, players, now=now)
        tws.append(TournamentWatch(tid, players, snap))
    return WatchSession(guild_id=guild_id, channel_id=channel_id, message_id=1, tournament_watches=tws)


async def test_all_rounds_reported_does_not_conclude(feeds):
    with _patch_fetch(_fake_fetch()):  # every result in, but melee.gg doesn't say Ended
        session = await _watch(feeds, GUILD_A, CHANNEL_A, {TID: PLAYERS_A})
        for i in range(1, 6):
            now = T0 + _minutes(i)
            update = feeds.update_watch(session, await feeds.poll(now), now)
            assert not update.concluded and not update.finished


async def test_melee_status_ended_concludes(feeds):
    with _patch_fetch(_fake_fetch()):
        session = await _watch(feeds, GUILD_A, CHANNEL_A, {TID: PLAYERS_A})
    now = T0 + _minutes(1)
    with _patch_fetch(_fetch_with_status("Ended")):
        update = feeds.update_watch(session, await feeds.poll(now), now)
    assert [tw.tournament_id for tw in update.concluded] == [TID]
    assert session.tournament_watches[0].concluded == CONCLUDED_ENDED
    assert update.finished is True
    assert TID not in feeds.feeds  # nobody left watching: no more fetches


async def test_final_results_arriving_with_ended_are_still_reported(feeds):
    with _patch_fetch(_fake_fetch("Not reported")):
        session = await _watch(feeds, GUILD_A, CHANNEL_A, {TID: ["Alice"]})
    now = T0 + _minutes(1)
    with _patch_fetch(_fetch_with_status("Ended", result="2-1")):
        update = feeds.update_watch(session, await feeds.poll(now), now)
    assert update.finished
    assert [c.description for c in update.changes] == ["Round 1 vs Zed: **2-1**"]


async def test_five_days_without_changes_concludes(feeds):
    with _patch_fetch(_fake_fetch()):
        session = await _watch(feeds, GUILD_A, CHANNEL_A, {TID: PLAYERS_A})
        almost = T0 + STALE_AFTER - _minutes(1)
        update = feeds.update_watch(session, await feeds.poll(almost), almost)
        assert not update.concluded

        after = T0 + STALE_AFTER
        update = feeds.update_watch(session, await feeds.poll(after), after)
    assert session.tournament_watches[0].concluded == CONCLUDED_STALE
    assert update.finished


async def test_a_change_restarts_the_five_day_clock(feeds):
    with _patch_fetch(_fake_fetch("Not reported")):
        session = await _watch(feeds, GUILD_A, CHANNEL_A, {TID: PLAYERS_A})
    day3 = T0 + datetime.timedelta(days=3)
    with _patch_fetch(_fake_fetch("2-0")):  # results come in on day 3
        feeds.update_watch(session, await feeds.poll(day3), day3)
        assert feeds.feeds[TID].last_change_at == day3
        day6 = T0 + datetime.timedelta(days=6)
        update = feeds.update_watch(session, await feeds.poll(day6), day6)
        assert not update.concluded
        day8 = day3 + STALE_AFTER
        update = feeds.update_watch(session, await feeds.poll(day8), day8)
    assert session.tournament_watches[0].concluded == CONCLUDED_STALE


async def test_stale_tournament_concludes_even_while_fetches_keep_failing(feeds):
    with _patch_fetch(_fake_fetch()):
        session = await _watch(feeds, GUILD_A, CHANNEL_A, {TID: PLAYERS_A})
    after = T0 + STALE_AFTER
    with _patch_fetch(AsyncMock(return_value=TournamentSnapshot(TID, error="melee.gg down"))):
        refreshed = await feeds.poll(after)
    assert refreshed == set()
    assert feeds.update_watch(session, refreshed, after).finished


async def test_another_watch_joining_with_new_players_is_not_a_change(feeds):
    with _patch_fetch(_fake_fetch()):
        await _watch(feeds, GUILD_A, CHANNEL_A, {TID: PLAYERS_A})
        await _watch(feeds, GUILD_B, CHANNEL_B, {TID: PLAYERS_B}, now=T0 + _minutes(30))
    assert feeds.feeds[TID].last_change_at == T0


async def test_last_change_survives_a_restart(feeds):
    await db.add_tournament(GUILD_A, TID, "u")
    with _patch_fetch(_fake_fetch()):
        await _watch(feeds, GUILD_A, CHANNEL_A, {TID: PLAYERS_A})
    assert await db.get_tournament_last_change(TID) == T0

    restarted = FeedManager(feeds.scheduler)
    four_days = T0 + datetime.timedelta(days=4)
    with _patch_fetch(_fake_fetch()):
        await restarted.join(CHANNEL_A, TID, PLAYERS_A, now=four_days, resuming=True)
    assert restarted.feeds[TID].last_change_at == T0
    assert restarted.feeds[TID].conclusion(T0 + STALE_AFTER) == CONCLUDED_STALE


async def test_starting_a_new_watch_on_an_old_tournament_gets_a_fresh_clock(feeds):
    await db.add_tournament(GUILD_A, TID, "u")
    await db.set_tournament_last_change(TID, T0 - datetime.timedelta(days=30))
    with _patch_fetch(_fake_fetch()):
        await feeds.join(CHANNEL_A, TID, PLAYERS_A, now=T0)
    assert feeds.feeds[TID].conclusion(T0) is None
    assert await db.get_tournament_last_change(TID) == T0


async def test_each_server_concludes_the_shared_tournament_for_itself(feeds):
    with _patch_fetch(_fake_fetch()):
        session_a = await _watch(feeds, GUILD_A, CHANNEL_A, {TID: PLAYERS_A})
        session_b = await _watch(feeds, GUILD_B, CHANNEL_B, {TID: PLAYERS_B})
    now = T0 + _minutes(5)
    fetch = _fetch_with_status("Ended")
    with _patch_fetch(fetch):
        refreshed = await feeds.poll(now)
    assert fetch.call_count == 1

    update_a = feeds.update_watch(session_a, refreshed, now)
    assert update_a.finished
    assert session_b.tournament_watches[0].concluded is None  # B decides on its own update
    update_b = feeds.update_watch(session_b, refreshed, now)
    assert update_b.finished and update_b.concluded[0].players == PLAYERS_B


async def test_watch_finishes_only_when_all_its_tournaments_have_concluded(feeds):
    other = TID + 1

    async def fetch(tournament_id, players, cached_rounds=None):
        snap = _snapshot(tournament_id, players)
        snap.status = "Ended" if tournament_id == TID else "In Progress"
        return snap

    with _patch_fetch(AsyncMock(side_effect=fetch)):
        session = await _watch(feeds, GUILD_A, CHANNEL_A, {TID: PLAYERS_A, other: PLAYERS_A})
        now = T0 + _minutes(5)
        update = feeds.update_watch(session, await feeds.poll(now), now)
    assert [tw.tournament_id for tw in update.concluded] == [TID]
    assert update.finished is False
    assert set(feeds.feeds) == {other}  # the ended one is no longer polled

    later = now + _minutes(1)
    with _patch_fetch(_fetch_with_status("Ended")):
        update = feeds.update_watch(session, await feeds.poll(later), later)
    assert [tw.tournament_id for tw in update.concluded] == [other]
    assert update.finished is True


# ── Active / Idle ──────────────────────────────────────────────────────────────

async def _poll_every_minute(feeds, start, minutes: int) -> int:
    """Tick the poll loop once a minute, as the bot does. Returns fetch-producing ticks."""
    hits = 0
    for i in range(minutes):
        hits += bool(await feeds.poll(start + _minutes(i)))
    return hits


async def test_active_feed_is_polled_every_minute(feeds):
    feeds.subscribe(CHANNEL_A, TID, PLAYERS_A)
    fetch = _fake_fetch("Not reported")
    with _patch_fetch(fetch):
        await feeds.poll(T0)
        await feeds.poll(T0 + datetime.timedelta(seconds=30))  # too soon
        await feeds.poll(T0 + ACTIVE_POLL)
    assert fetch.call_count == 2
    assert feeds.feeds[TID].activity is Activity.ACTIVE


async def test_feed_goes_idle_after_90_quiet_minutes_and_then_polls_every_15(feeds):
    feeds.subscribe(CHANNEL_A, TID, PLAYERS_A)
    fetch = _fake_fetch()  # all results in, nothing changing
    with _patch_fetch(fetch):
        await _poll_every_minute(feeds, T0, 90)
        feed = feeds.feeds[TID]
        assert feed.activity is Activity.ACTIVE
        assert fetch.call_count == 90

        went_idle_at = T0 + IDLE_AFTER
        await feeds.poll(went_idle_at)
        assert feed.activity is Activity.IDLE
        assert feed.next_poll_at == went_idle_at + IDLE_POLL

        fetch.reset_mock()
        assert await _poll_every_minute(feeds, went_idle_at + _minutes(1), 60) == 4
    assert fetch.call_count == 4


async def test_round_in_progress_keeps_the_feed_active(feeds):
    feeds.subscribe(CHANNEL_A, TID, PLAYERS_A)
    with _patch_fetch(_fake_fetch("Not reported")):  # paired, results outstanding
        await feeds.poll(T0)
        await feeds.poll(T0 + IDLE_AFTER * 2)
    assert feeds.feeds[TID].activity is Activity.ACTIVE


def _two_rounds(tournament_id, players, cached_rounds=None) -> TournamentSnapshot:
    snap = _snapshot(tournament_id, players)
    for ps in snap.players.values():
        ps.rounds["Round 2"] = "Not reported"
        ps.opponents["Round 2"] = "Yan"
    snap.rounds_started = 2
    return snap


async def test_new_round_makes_an_idle_feed_active_again(feeds):
    with _patch_fetch(_fake_fetch()):
        session = await _watch(feeds, GUILD_A, CHANNEL_A, {TID: PLAYERS_A})
        idle_at = T0 + IDLE_AFTER
        feeds.update_watch(session, await feeds.poll(idle_at), idle_at)
    feed = feeds.feeds[TID]
    assert feed.activity is Activity.IDLE and session.idle is True

    next_morning = idle_at + datetime.timedelta(hours=1)
    with _patch_fetch(AsyncMock(side_effect=_two_rounds)):
        update = feeds.update_watch(session, await feeds.poll(next_morning), next_morning)
    assert feed.activity is Activity.ACTIVE
    assert feed.next_poll_at == next_morning + ACTIVE_POLL
    assert session.idle is False
    assert {c.description for c in update.changes} == {"Round 2 paired vs Yan"}


async def test_tournament_that_has_not_started_idles_without_counting_as_a_failure(feeds):
    not_started = TournamentSnapshot(TID, error="No rounds have started yet.", not_started=True)
    fetch = AsyncMock(return_value=not_started)
    with _patch_fetch(fetch):
        session = await _watch(feeds, GUILD_A, CHANNEL_A, {TID: PLAYERS_A})
        feed = feeds.feeds[TID]
        assert feed.activity is Activity.IDLE
        assert feed.failures == 0
        assert feed.next_poll_at == T0 + IDLE_POLL
        assert session.tournament_watches[0].snapshot.not_started is True

        assert await _poll_every_minute(feeds, T0 + _minutes(1), 30) == 0
        assert fetch.call_count == 3  # the join, then two idle polls 15 minutes apart

    start = T0 + datetime.timedelta(hours=20)
    with _patch_fetch(_fake_fetch("Not reported")):
        update = feeds.update_watch(session, await feeds.poll(start), start)
    assert feed.activity is Activity.ACTIVE
    assert [c.description for c in update.changes] == ["Round 1 paired vs Zed"] * 2


async def test_second_watch_joining_an_unstarted_tournament_does_not_refetch(feeds):
    fetch = AsyncMock(return_value=TournamentSnapshot(TID, error="No rounds", not_started=True))
    with _patch_fetch(fetch):
        await feeds.join(CHANNEL_A, TID, PLAYERS_A, now=T0)
        snap = await feeds.join(CHANNEL_B, TID, PLAYERS_B, now=T0 + _minutes(1))
    assert fetch.call_count == 1
    assert snap.not_started is True


async def test_feeds_start_out_active_after_a_restart(feeds):
    with _patch_fetch(_fake_fetch()):
        await _watch(feeds, GUILD_A, CHANNEL_A, {TID: PLAYERS_A})
        await feeds.poll(T0 + IDLE_AFTER)
        assert feeds.feeds[TID].activity is Activity.IDLE

        restarted = FeedManager(feeds.scheduler)
        later = T0 + datetime.timedelta(hours=10)
        await restarted.join(CHANNEL_A, TID, PLAYERS_A, now=later, resuming=True)
        feed = restarted.feeds[TID]
        assert feed.activity is Activity.ACTIVE
        await restarted.poll(later + IDLE_AFTER)  # ...and settles on its own
        assert feed.activity is Activity.IDLE


# ── what the watch's embed should do ───────────────────────────────────────────

async def test_watch_reports_going_idle_exactly_once(feeds):
    with _patch_fetch(_fake_fetch()):
        session = await _watch(feeds, GUILD_A, CHANNEL_A, {TID: PLAYERS_A})
        t = T0 + _minutes(1)
        assert feeds.update_watch(session, await feeds.poll(t), t).went_idle is False

        t = T0 + IDLE_AFTER
        update = feeds.update_watch(session, await feeds.poll(t), t)
        assert update.went_idle is True and session.idle is True
        assert not update.changes and not update.concluded

        t += IDLE_POLL
        assert feeds.update_watch(session, await feeds.poll(t), t).went_idle is False
        assert session.idle is True


async def test_watch_is_idle_only_when_all_its_live_tournaments_are(feeds):
    other = TID + 1

    async def fetch(tournament_id, players, cached_rounds=None):
        return _snapshot(tournament_id, players, "2-0" if tournament_id == TID else "Not reported")

    with _patch_fetch(AsyncMock(side_effect=fetch)):
        session = await _watch(feeds, GUILD_A, CHANNEL_A, {TID: PLAYERS_A, other: PLAYERS_A})
        t = T0 + IDLE_AFTER
        update = feeds.update_watch(session, await feeds.poll(t), t)
    assert feeds.feeds[TID].activity is Activity.IDLE
    assert feeds.feeds[other].activity is Activity.ACTIVE
    assert session.idle is False and update.went_idle is False


async def test_change_after_a_long_pause_asks_for_a_fresh_embed(feeds):
    with _patch_fetch(_fake_fetch()):
        session = await _watch(feeds, GUILD_A, CHANNEL_A, {TID: PLAYERS_A})
        feeds.update_watch(session, await feeds.poll(T0), T0)
    resumed = T0 + FRESH_EMBED_AFTER + _minutes(1)
    with _patch_fetch(AsyncMock(side_effect=_two_rounds)):
        update = feeds.update_watch(session, await feeds.poll(resumed), resumed)
    assert update.changes and update.repost is True

    # the next change, minutes later, edits the new embed in place
    soon = resumed + _minutes(1)

    async def round_two_done(tournament_id, players, cached_rounds=None):
        snap = _two_rounds(tournament_id, players)
        for ps in snap.players.values():
            ps.rounds["Round 2"] = "2-1"
        return snap

    with _patch_fetch(AsyncMock(side_effect=round_two_done)):
        update = feeds.update_watch(session, await feeds.poll(soon), soon)
    assert update.changes and update.repost is False


async def test_change_after_a_short_pause_edits_in_place(feeds):
    with _patch_fetch(_fake_fetch()):
        session = await _watch(feeds, GUILD_A, CHANNEL_A, {TID: PLAYERS_A})
        feeds.update_watch(session, await feeds.poll(T0), T0)
    later = T0 + FRESH_EMBED_AFTER - _minutes(1)
    with _patch_fetch(AsyncMock(side_effect=_two_rounds)):
        update = feeds.update_watch(session, await feeds.poll(later), later)
    assert update.changes and update.repost is False
