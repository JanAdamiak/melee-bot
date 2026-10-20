"""Shared polling state for /watch: one fetch per tournament.

Several watches — in different channels or different servers — can include
the same tournament, each with its own players. A TournamentFeed is the one
place that tournament is fetched from melee.gg: it fetches once for the
combined player list, and every watch then takes its own players out of
that shared result.
"""

import asyncio
import datetime
import enum
import logging
from dataclasses import dataclass, field

from domain import db as database
from domain import tracker
from domain.tracker import Change, TournamentSnapshot, WatchSession
from scheduler import PRIORITY_WATCH, FetchScheduler

log = logging.getLogger(__name__)

_BACKOFF_BASE_SECONDS = 60
_BACKOFF_MAX_SECONDS = 30 * 60
# Each feed carries its own next-poll time. An Active feed is polled every
# minute; an Idle one — nothing has changed for a while and no round is in
# progress, e.g. overnight between two days of play — every 15 minutes.
ACTIVE_POLL = datetime.timedelta(seconds=60)
IDLE_POLL = datetime.timedelta(minutes=15)
IDLE_AFTER = datetime.timedelta(minutes=90)
# While a /topcut session is in bubble mode, Active feeds are polled this many
# times less often so its fetches spend less time queued behind /watch polls.
_BUBBLE_SLOWDOWN = 3
# The poll loop ticks once a minute; without some slack a feed due "in 60s"
# could be judged a few milliseconds early and wait a whole extra tick.
_DUE_SLACK = datetime.timedelta(seconds=5)
# After a quiet spell this long, a watch posts a fresh embed for its next
# change instead of editing one that has scrolled far out of view.
FRESH_EMBED_AFTER = datetime.timedelta(hours=6)
# A watch that starts on an already-polled tournament reuses the feed's
# snapshot when it is at most this old, instead of fetching again.
REUSE_SNAPSHOT_SECONDS = 90

# A watch concludes a tournament when melee.gg shows it as ended, or when
# nothing about it has changed for this long (abandoned or cancelled events
# never reach "Ended"). `/watch stop` is the third, manual way out.
STALE_AFTER = datetime.timedelta(days=5)
CONCLUDED_ENDED = "ended"
CONCLUDED_STALE = "stale"


class Activity(enum.Enum):
    ACTIVE = "active"
    IDLE = "idle"


@dataclass
class TournamentFeed:
    tournament_id: int
    # watch key (the watch's channel ID) -> the players that watch tracks
    subscribers: dict[int, list[str]] = field(default_factory=dict)
    # Latest result for the combined player list. Stays on the last good
    # snapshot through failed fetches; only holds an error if no fetch has
    # succeeded yet.
    snapshot: TournamentSnapshot | None = None
    fetched_at: datetime.datetime | None = None
    failures: int = 0
    # When to fetch next: the Active/Idle interval, or the failure backoff.
    next_poll_at: datetime.datetime | None = None
    activity: Activity = Activity.ACTIVE
    # Start of the current quiet spell, for going Idle. Unlike last_change_at
    # this is not persisted: after a restart a feed starts out Active and
    # settles on its own.
    quiet_since: datetime.datetime | None = None
    # (start_time, status) as last written to the tournaments table
    stored_info: tuple[datetime.datetime | None, str | None] = (None, None)
    # When anything last changed for the combined player list; persisted in
    # the tournaments table so the 5-day clock survives restarts.
    last_change_at: datetime.datetime | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def players(self) -> list[str]:
        """Every player any subscribed watch tracks, de-duplicated."""
        return sorted({p for players in self.subscribers.values() for p in players})

    def covers(self, players: list[str]) -> bool:
        """True if the latest good snapshot was fetched for all of `players`."""
        if self.snapshot is None:
            return False
        if self.snapshot.not_started:
            return True  # nothing player-specific to fetch yet
        if self.snapshot.error:
            return False
        return all(p in self.snapshot.players for p in players)

    def conclusion(self, now: datetime.datetime) -> str | None:
        """Why watches of this tournament should conclude now, or None."""
        if self.snapshot is not None and self.snapshot.ended:
            return CONCLUDED_ENDED
        if self.last_change_at is not None and now - self.last_change_at >= STALE_AFTER:
            return CONCLUDED_STALE
        return None

    def view(self, players: list[str]) -> TournamentSnapshot:
        """The shared snapshot narrowed to one watch's own players."""
        if self.snapshot is None:
            return TournamentSnapshot(self.tournament_id, error="Not fetched yet.")
        snap = self.snapshot
        return TournamentSnapshot(
            tournament_id=snap.tournament_id,
            players={p: snap.players[p] for p in players if p in snap.players},
            error=snap.error,
            last_round_is_final=snap.last_round_is_final,
            rounds_started=snap.rounds_started,
            name=snap.name,
            start_time=snap.start_time,
            status=snap.status,
            not_started=snap.not_started,
        )


def _changed(old: TournamentSnapshot | None, new: TournamentSnapshot) -> bool:
    """Whether a good new snapshot differs from the one before it. The very
    first fetch is not a change; rounds appearing after "not started" is."""
    if old is None:
        return False
    if old.error or old.rounds_started != new.rounds_started:
        return True
    # Compare only players both snapshots were fetched for, so a watch joining
    # with new players doesn't read as tournament activity.
    common = TournamentSnapshot(
        new.tournament_id, players={p: ps for p, ps in new.players.items() if p in old.players}
    )
    return bool(tracker.diff(old, common))


@dataclass
class WatchUpdate:
    changes: list[Change] = field(default_factory=list)
    # tournaments that concluded for this watch on this tick
    concluded: list[tracker.TournamentWatch] = field(default_factory=list)
    # every tournament in the watch has now concluded
    finished: bool = False
    # the watch just went quiet: show the "waiting" notice
    went_idle: bool = False
    # these changes end a long pause: post a fresh embed instead of editing
    repost: bool = False


def _round_in_progress(snap: TournamentSnapshot) -> bool:
    return any(
        result == "Not reported" for ps in snap.players.values() for result in ps.rounds.values()
    )


class FeedManager:
    """Owns every TournamentFeed and is the only caller of fetch_snapshot."""

    def __init__(self, scheduler: FetchScheduler) -> None:
        self.scheduler = scheduler
        self.feeds: dict[int, TournamentFeed] = {}

    def subscribe(self, watch_key: int, tournament_id: int, players: list[str]) -> TournamentFeed:
        feed = self.feeds.setdefault(tournament_id, TournamentFeed(tournament_id))
        feed.subscribers[watch_key] = list(players)
        return feed

    def unsubscribe(self, watch_key: int, tournament_id: int | None = None) -> None:
        """Remove a watch from one tournament's feed, or from all of them.
        A feed nobody watches any more is dropped."""
        for tid in [tournament_id] if tournament_id is not None else list(self.feeds):
            feed = self.feeds.get(tid)
            if feed is None:
                continue
            feed.subscribers.pop(watch_key, None)
            if not feed.subscribers:
                del self.feeds[tid]

    async def join(
        self,
        watch_key: int,
        tournament_id: int,
        players: list[str],
        now: datetime.datetime | None = None,
        resuming: bool = False,
    ) -> TournamentSnapshot:
        """Subscribe a watch and return its first snapshot.

        Reuses the feed's snapshot if it is recent and already includes these
        players; otherwise fetches through the scheduler like any other poll.

        resuming: the watch is being restored after a restart, so the
        tournament's stored last-change time still applies. A watch someone
        has just started instead gets a fresh 5-day clock, unless another
        watch is already polling the tournament.
        """
        now = now or datetime.datetime.now(datetime.UTC)
        feed = self.subscribe(watch_key, tournament_id, players)
        async with feed.lock:
            if feed.last_change_at is None:
                stored = await database.get_tournament_last_change(tournament_id) if resuming else None
                feed.last_change_at = stored or now
                if stored is None:
                    await database.set_tournament_last_change(tournament_id, now)
            recent = (
                feed.fetched_at is not None
                and (now - feed.fetched_at).total_seconds() <= REUSE_SNAPSHOT_SECONDS
            )
            if not (recent and feed.covers(players)):
                await self._refresh(feed, now)
        return feed.view(players)

    async def poll(self, now: datetime.datetime, bubble_active: bool = False) -> set[int]:
        """Refresh every feed that is due. Returns the tournament IDs that got
        a fresh snapshot, so watches know which of their tournaments to re-diff."""
        refreshed: set[int] = set()
        for feed in list(self.feeds.values()):
            if not feed.subscribers:
                continue
            if feed.next_poll_at and now < feed.next_poll_at - _DUE_SLACK:
                continue  # not due yet: idle, or backing off after failures
            try:
                async with feed.lock:
                    ok = await self._refresh(feed, now, bubble_active)
            except Exception as e:
                log.error("Poll error tournament=%s: %s", feed.tournament_id, e)
                continue
            if ok:
                refreshed.add(feed.tournament_id)
        return refreshed

    async def _refresh(
        self, feed: TournamentFeed, now: datetime.datetime, bubble_active: bool = False,
    ) -> bool:
        """One scheduler fetch for the feed's combined player list."""
        tid = feed.tournament_id
        players = feed.players
        cached = await database.load_round_results(tid)
        new_snap = await self.scheduler.submit(
            PRIORITY_WATCH, lambda: tracker.fetch_snapshot(tid, players, cached)
        )

        # The page's start time and status are known even when no round has
        # started; keep the stored copy in step, writing only on a change.
        info = (new_snap.start_time, new_snap.status)
        if any(info) and info != feed.stored_info:
            await database.set_tournament_info(tid, *info)
            feed.stored_info = info

        if new_snap.error and not new_snap.not_started:
            feed.failures += 1
            backoff = min(_BACKOFF_BASE_SECONDS * 2 ** feed.failures, _BACKOFF_MAX_SECONDS)
            feed.next_poll_at = now + datetime.timedelta(seconds=backoff)
            if feed.snapshot is None or feed.snapshot.error:
                feed.snapshot = new_snap
            log.warning(
                "tournament=%s fetch failed (%s consecutive) — backing off %ss",
                tid, feed.failures, backoff,
            )
            return False
        feed.failures = 0
        feed.fetched_at = now
        if feed.quiet_since is None:
            feed.quiet_since = now

        if new_snap.not_started:
            # Nothing to watch minute by minute until round 1 is paired.
            if feed.snapshot is None or feed.snapshot.error:
                feed.snapshot = new_snap
            self._set_activity(feed, Activity.IDLE)
            feed.next_poll_at = now + IDLE_POLL
            return False

        changed = _changed(feed.snapshot, new_snap)
        if changed:
            feed.last_change_at = now
            feed.quiet_since = now
            await database.set_tournament_last_change(tid, now)
        feed.snapshot = new_snap

        # Any change (a new round being paired included) makes the feed
        # Active again; it goes Idle once nothing has changed for IDLE_AFTER
        # and no tracked player is still waiting on a result.
        if changed or _round_in_progress(new_snap):
            self._set_activity(feed, Activity.ACTIVE)
        elif now - feed.quiet_since >= IDLE_AFTER:
            self._set_activity(feed, Activity.IDLE)

        if feed.activity is Activity.IDLE:
            feed.next_poll_at = now + IDLE_POLL
        else:
            feed.next_poll_at = now + ACTIVE_POLL * (_BUBBLE_SLOWDOWN if bubble_active else 1)

        finalized = {
            player: {
                rnd: (result, ps.opponents.get(rnd, "?"))
                for rnd, result in ps.rounds.items()
                if result != "Not reported"
            }
            for player, ps in new_snap.players.items()
        }
        if finalized:
            await database.save_round_results(tid, finalized)
        return True

    @staticmethod
    def _set_activity(feed: TournamentFeed, activity: Activity) -> None:
        if feed.activity is not activity:
            log.info("tournament=%s is now %s", feed.tournament_id, activity.value)
            feed.activity = activity

    def update_watch(
        self, session: WatchSession, refreshed: set[int], now: datetime.datetime,
    ) -> WatchUpdate:
        """Bring one watch up to date with the feeds after a poll.

        Each of its live tournaments that was refreshed gets its own players'
        slice of the new snapshot and is diffed against the previous one.
        Tournaments that melee.gg now shows as ended, or that have been silent
        for STALE_AFTER, conclude for this watch and leave their feed; the
        watch as a whole is finished once all of its tournaments have.

        Also tracks whether the watch as a whole has gone quiet (all of its
        live feeds Idle), and whether new changes end a pause long enough
        that they deserve a fresh embed.
        """
        update = WatchUpdate()
        if session.last_change_at is None:
            session.last_change_at = now
        for tw in session.tournament_watches:
            feed = self.feeds.get(tw.tournament_id)
            if tw.concluded or feed is None:
                continue
            if tw.tournament_id in refreshed:
                new_snap = feed.view(tw.players)
                update.changes.extend(tracker.diff(tw.snapshot, new_snap))
                tw.snapshot = new_snap
            reason = feed.conclusion(now)
            if reason:
                tw.concluded = reason
                update.concluded.append(tw)
                self.unsubscribe(session.channel_id, tw.tournament_id)
        update.finished = all(tw.concluded for tw in session.tournament_watches)

        live = [self.feeds[tw.tournament_id] for tw in session.tournament_watches if not tw.concluded]
        idle = bool(live) and all(feed.activity is Activity.IDLE for feed in live)
        update.went_idle = idle and not session.idle
        session.idle = idle

        if update.changes:
            update.repost = now - session.last_change_at > FRESH_EMBED_AFTER
            session.last_change_at = now
        return update
