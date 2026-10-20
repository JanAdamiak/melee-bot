import datetime
import logging
import os

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

import config
from commands.discovery import (
    BindSuggestionButton,
    DiscoveryGroup,
    IgnoreSuggestionButton,
    suggestion_view,
)
from commands.player import PlayerGroup
from commands.topcut import TopCutGroup
from commands.tournament import TournamentGroup
from commands.views import MAX_TOURNAMENTS
from commands.watch import WatchGroup
from domain import db as database
from domain import discovery, topcut, tracker
from domain.feed import FeedManager
from domain.tracker import TournamentWatch, WatchSession, build_embed
from melee_tiebreakers.melee_client import get_player_tournaments
from scheduler import (
    PRIORITY_DISCOVERY,
    PRIORITY_TOPCUT_BUBBLE,
    PRIORITY_TOPCUT_NORMAL,
    FetchScheduler,
)

load_dotenv()
log = logging.getLogger(__name__)
_LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=_LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s")


class MeleeBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.watches: dict[int, WatchSession] = {}
        self.topcut_sessions: dict[int, topcut.TopCutSession] = {}
        self.scheduler = FetchScheduler()
        self.feeds = FeedManager(self.scheduler)
        self.db = database

    def update_tournament_name(self, tournament_id: int, name: str | None) -> None:
        """Refresh the in-memory title for any active watch on this tournament."""
        for session in self.watches.values():
            for tw in session.tournament_watches:
                if tw.tournament_id == tournament_id:
                    tw.name = name

    async def all_display_names(self, guild_id: int) -> dict[str, str]:
        """One server's display names: those held by its active sessions,
        overlaid with its persisted roster (source of truth)."""
        names: dict[str, str] = {}
        for session in self.watches.values():
            if session.guild_id != guild_id:
                continue
            for tw in session.tournament_watches:
                names.update(tw.display_names)
        names.update(await database.get_display_names(guild_id))
        return names

    async def setup_hook(self) -> None:
        guild_ids = config.guild_ids()
        if not guild_ids:
            raise RuntimeError("GUILD_IDS not set in environment.")
        await database.init_db(legacy_guild_id=config.legacy_guild_id())
        self.tree.add_command(WatchGroup(self))
        self.tree.add_command(PlayerGroup(self))
        self.tree.add_command(TournamentGroup(self))
        self.tree.add_command(TopCutGroup(self))
        self.tree.add_command(DiscoveryGroup(self))
        # Suggestion buttons carry their tournament ID, so they work after a restart.
        self.add_dynamic_items(BindSuggestionButton, IgnoreSuggestionButton)
        for guild_id in guild_ids:
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            try:
                await self.tree.sync(guild=guild)
            except discord.Forbidden:
                # Not invited there (yet) — don't let one server block the rest.
                log.warning("Could not sync commands to guild %s: bot is not a member.", guild_id)
                continue
            log.info("Synced commands to guild %s.", guild_id)
        await self.scheduler.start()
        self.poll_task.start()
        self.topcut_poll_task.start()
        self.discovery_task.start()

    async def on_ready(self) -> None:
        await self._resume_sessions()
        await self._resume_topcut_sessions()

    async def _open_tournament_watch(
        self, guild_id: int, channel_id: int, tournament_id: int, display_names: dict[str, str],
        resuming: bool = False,
    ) -> TournamentWatch | None:
        """Subscribe a channel's watch to a tournament's shared feed and build
        its TournamentWatch. None if the server has no players bound to it."""
        players = await database.get_tournament_players(guild_id, tournament_id)
        if not players:
            return None
        snap = await self.feeds.join(channel_id, tournament_id, players, resuming=resuming)
        rounds_to_track = await database.get_tournament_rounds_to_track(guild_id, tournament_id)
        name = await database.get_tournament_name(tournament_id)
        return TournamentWatch(
            tournament_id, players, snap, display_names,
            rounds_to_track=rounds_to_track, name=name,
        )

    async def bind_tournament(
        self, guild_id: int, tournament_id: int, added_by: str, rounds_to_track: int | None = None,
    ) -> str | None:
        """Bind a tournament to a server and return its name. Loads the
        tournament's page (once) only if its name or start time isn't stored yet."""
        info = await database.get_tournament_info(tournament_id)
        name = info["name"] if info else None
        page = None
        if info is None or name is None or info["start_time"] is None:
            try:
                page = await tracker.get_tournament_page(tournament_id)
            except Exception as e:
                log.warning("Could not load tournament %s page while binding: %s", tournament_id, e)
        if page and name is None:
            name = page.name
        await database.add_tournament(guild_id, tournament_id, added_by, rounds_to_track, name)
        if page:
            await database.set_tournament_info(tournament_id, page.start_time, page.status)
        if rounds_to_track is not None:
            await database.set_tournament_rounds_to_track(guild_id, tournament_id, rounds_to_track)
        return name

    async def drop_watch(self, channel_id: int) -> WatchSession | None:
        """Forget a channel's watch: in memory, in its feeds and in the DB."""
        session = self.watches.pop(channel_id, None)
        self.feeds.unsubscribe(channel_id)
        await database.delete_watch_session(channel_id)
        return session

    async def _resume_sessions(self) -> None:
        sessions = [s for s in await database.load_watch_sessions() if s[1] not in self.watches]
        # Subscribe every session before fetching anything, so a tournament
        # that several of them share is fetched once for all their players.
        for guild_id, channel_id, _, tournament_ids in sessions:
            for tid in tournament_ids:
                players = await database.get_tournament_players(guild_id, tid)
                if players:
                    self.feeds.subscribe(channel_id, tid, players)
        for guild_id, channel_id, message_id, tournament_ids in sessions:
            display_names = await database.get_display_names(guild_id)
            tournament_watches: list[TournamentWatch] = []
            for tid in tournament_ids:
                tw = await self._open_tournament_watch(
                    guild_id, channel_id, tid, display_names, resuming=True
                )
                if tw is None:
                    log.warning("Skipping tournament %s on resume: no players bound", tid)
                    continue
                tournament_watches.append(tw)
            if not tournament_watches:
                await database.delete_watch_session(channel_id)
                continue
            session = WatchSession(
                guild_id=guild_id,
                channel_id=channel_id,
                message_id=message_id,
                tournament_watches=tournament_watches,
            )
            await self._post_watch_embed(session, build_embed(session, []))
            self.watches[channel_id] = session
            log.info(
                "Resumed watch guild=%s channel=%s tournaments=%s", guild_id, channel_id, tournament_ids
            )

    async def _post_watch_embed(
        self, session: WatchSession, embed: discord.Embed, fresh: bool = False,
    ) -> None:
        """Show a watch's embed by editing its message, or — with fresh=True,
        or if the message is gone — by posting a new one. A message that is
        replaced while it still exists is left pointing at its successor."""
        channel = self.get_channel(session.channel_id)
        if not channel:
            return
        old = None
        try:
            old = await channel.fetch_message(session.message_id)
        except discord.NotFound:
            pass
        if old is not None and not fresh:
            await old.edit(embed=embed)
            return
        msg = await channel.send(embed=embed)
        session.message_id = msg.id
        await database.update_watch_session_message(session.channel_id, msg.id)
        if old is not None and old.embeds:
            retired = old.embeds[0]
            retired.color = discord.Color.greyple()
            retired.set_footer(text="Continued below ⬇")
            try:
                await old.edit(embed=retired)
            except discord.HTTPException as e:
                log.warning("Could not mark old embed as continued channel=%s: %s", session.channel_id, e)

    async def _resume_topcut_sessions(self) -> None:
        sessions = await database.load_topcut_sessions()
        for guild_id, channel_id, message_id, tournament_id, cut_size, started_by in sessions:
            if channel_id in self.topcut_sessions:
                continue
            name = await database.get_tournament_name(tournament_id)
            session = topcut.TopCutSession(
                guild_id=guild_id, channel_id=channel_id, message_id=message_id,
                tournament_id=tournament_id, cut_size=cut_size, started_by=started_by,
                tournament_name=name,
            )
            await self.scheduler.submit(PRIORITY_TOPCUT_NORMAL, lambda s=session: topcut.refresh_session(s))
            self.scheduler.set_bubble(tournament_id, session.bubble)
            await self._post_topcut_embed(session)
            self.topcut_sessions[channel_id] = session
            log.info("Resumed topcut channel=%s tournament=%s", channel_id, tournament_id)

    async def _post_topcut_embed(self, session: topcut.TopCutSession) -> None:
        channel = self.get_channel(session.channel_id)
        if not channel:
            return
        display_names = await self.all_display_names(session.guild_id)
        embed = topcut.build_topcut_embed(session, display_names)
        try:
            msg = await channel.fetch_message(session.message_id)
            await msg.edit(embed=embed)
        except discord.NotFound:
            msg = await channel.send(embed=embed)
            session.message_id = msg.id
            await database.update_topcut_session_message(session.channel_id, msg.id)

    async def close(self) -> None:
        await super().close()

    async def _create_watch(
        self, guild_id: int, channel: discord.abc.Messageable, tournament_ids: list[int],
    ) -> WatchSession | None:
        """Start a watch in a channel that has none. None if no tournament
        in the list has players bound in this server."""
        channel_id = channel.id
        display_names = await database.get_display_names(guild_id)
        tournament_watches: list[TournamentWatch] = []
        for tid in tournament_ids:
            tw = await self._open_tournament_watch(guild_id, channel_id, tid, display_names)
            if tw is not None:
                tournament_watches.append(tw)
        if not tournament_watches:
            self.feeds.unsubscribe(channel_id)
            return None

        session = WatchSession(
            guild_id=guild_id,
            channel_id=channel_id,
            message_id=0,
            tournament_watches=tournament_watches,
        )
        # A watch on tournaments that haven't started is waiting from the outset.
        session.idle = all(tw.snapshot.not_started for tw in tournament_watches)
        msg = await channel.send(embed=build_embed(session, []))
        session.message_id = msg.id
        self.watches[channel_id] = session
        await database.save_watch_session(
            guild_id, channel_id, msg.id, [tw.tournament_id for tw in tournament_watches]
        )
        return session

    async def start_watch(
        self,
        interaction: discord.Interaction,
        tournament_ids: list[int],
    ) -> None:
        if interaction.channel_id in self.watches:
            await interaction.followup.send(
                "A watch is already active in this channel. Use `/watch stop` first.",
                ephemeral=True,
            )
            return
        session = await self._create_watch(interaction.guild_id, interaction.channel, tournament_ids)
        if session is None:
            await interaction.followup.send(
                "None of those tournaments have players bound. Use `/tournament bind` first.",
                ephemeral=True,
            )
            return
        await interaction.followup.send("Watch started!", ephemeral=True)

    async def watch_in_channel(
        self, guild_id: int, channel: discord.abc.Messageable, tournament_id: int,
    ) -> str:
        """Watch a bound tournament in a channel: start a watch there, or add
        the tournament to the one already running. Returns a sentence saying
        what happened."""
        session = self.watches.get(channel.id)
        if session is None:
            session = await self._create_watch(guild_id, channel, [tournament_id])
            if session is None:
                return "It has no players bound, so no watch was started."
            return f"Started a watch in <#{channel.id}>."
        if any(tw.tournament_id == tournament_id for tw in session.tournament_watches):
            return f"It is already being watched in <#{channel.id}>."
        if len(session.tournament_watches) >= MAX_TOURNAMENTS:
            return (
                f"The watch in <#{channel.id}> already has {MAX_TOURNAMENTS} tournaments, "
                "so it wasn't added — start it later with `/watch start`."
            )
        display_names = await database.get_display_names(guild_id)
        tw = await self._open_tournament_watch(guild_id, channel.id, tournament_id, display_names)
        if tw is None:
            return "It has no players bound, so it wasn't added to the watch."
        session.tournament_watches.append(tw)
        await database.save_watch_session(
            guild_id, channel.id, session.message_id,
            [t.tournament_id for t in session.tournament_watches],
        )
        await self._post_watch_embed(session, build_embed(session, []))
        return f"Added it to the watch in <#{channel.id}>."

    # ── background tasks ──────────────────────────────────────────────────────

    @tasks.loop(seconds=60)
    async def poll_task(self) -> None:
        # First refresh each watched tournament once, for the combined player
        # list of every watch that includes it; then let each watch take its
        # own players out of the shared result.
        now = datetime.datetime.now(datetime.UTC)
        refreshed = await self.feeds.poll(now, self.scheduler.bubble_active)
        for channel_id, session in list(self.watches.items()):
            try:
                await self._update_session(channel_id, session, refreshed, now)
            except Exception as e:
                log.error(f"Poll error channel={channel_id}: {e}")

    @poll_task.before_loop
    async def _before_poll(self) -> None:
        await self.wait_until_ready()

    async def _update_session(
        self, channel_id: int, session: WatchSession, refreshed: set[int], now: datetime.datetime,
    ) -> None:
        update = self.feeds.update_watch(session, refreshed, now)
        # Concluding is per server: another server watching the same
        # tournament keeps its own flag.
        for tw in update.concluded:
            log.info(
                "Tournament %s concluded (%s) guild=%s channel=%s",
                tw.tournament_id, tw.concluded, session.guild_id, channel_id,
            )
            await database.set_tournament_concluded(session.guild_id, tw.tournament_id)

        if not update.changes and not update.concluded and not update.went_idle:
            return  # nothing new — don't touch the Discord message

        embed = build_embed(session, update.changes, concluded=update.finished)
        await self._post_watch_embed(session, embed, fresh=update.repost)

        if update.finished:
            log.info("Watch finished channel=%s: all its tournaments concluded.", channel_id)
            await self.drop_watch(channel_id)

    @tasks.loop(seconds=15)
    async def topcut_poll_task(self) -> None:
        now = datetime.datetime.now(datetime.UTC)
        for channel_id, session in list(self.topcut_sessions.items()):
            if session.next_attempt and now < session.next_attempt:
                continue
            priority = PRIORITY_TOPCUT_BUBBLE if session.bubble else PRIORITY_TOPCUT_NORMAL
            try:
                await self.scheduler.submit(priority, lambda s=session: topcut.refresh_session(s))
            except Exception as e:
                log.error(f"Topcut poll error channel={channel_id}: {e}")
                continue
            self.scheduler.set_bubble(session.tournament_id, session.bubble)
            try:
                await self._post_topcut_embed(session)
            except discord.HTTPException as e:
                log.error(f"Topcut embed post failed channel={channel_id}: {e}")
                continue
            if session.concluded:
                log.info(f"Topcut concluded channel={channel_id} tournament={session.tournament_id}")
                self.scheduler.set_bubble(session.tournament_id, False)
                await database.delete_topcut_session(channel_id)
                del self.topcut_sessions[channel_id]

    @topcut_poll_task.before_loop
    async def _before_topcut_poll(self) -> None:
        await self.wait_until_ready()


    @tasks.loop(hours=discovery.SCAN_INTERVAL_HOURS)
    async def discovery_task(self) -> None:
        try:
            await self._run_discovery()
        except Exception as e:
            log.error("Discovery scan failed: %s", e)

    @discovery_task.before_loop
    async def _before_discovery(self) -> None:
        await self.wait_until_ready()

    async def _run_discovery(self) -> None:
        # Only servers with discovery on and a channel the bot can still see.
        channels = {
            guild_id: channel
            for guild_id, channel_id in (await database.list_discovery_channels()).items()
            if (channel := self.get_channel(channel_id)) is not None
        }
        if not channels:
            return
        rosters = {guild_id: await database.list_players(guild_id) for guild_id in channels}

        def fetch(username: str):
            return self.scheduler.submit(PRIORITY_DISCOVERY, lambda: get_player_tournaments(username))

        results = await discovery.scan((p for roster in rosters.values() for p in roster), fetch)
        now = datetime.datetime.now(datetime.UTC)
        for guild_id, channel in channels.items():
            bound = {t["id"] for t in await database.list_tournaments(guild_id)}
            known = bound | await database.list_suggested_tournament_ids(guild_id)
            display_names = await database.get_display_names(guild_id)
            for suggestion in discovery.suggestions_for(rosters[guild_id], results, known, now):
                await self._post_suggestion(guild_id, channel, suggestion, display_names, now)

    async def _post_suggestion(
        self,
        guild_id: int,
        channel: discord.abc.Messageable,
        suggestion: discovery.Suggestion,
        display_names: dict[str, str],
        now: datetime.datetime,
    ) -> None:
        tid = suggestion.tournament.tournament_id
        # Claim the row first: it is what guarantees one message per tournament.
        if not await database.add_discovery_suggestion(guild_id, tid, suggestion.players):
            return
        embed = discovery.build_suggestion_embed(suggestion, display_names, now)
        try:
            msg = await channel.send(embed=embed, view=suggestion_view(tid))
        except discord.HTTPException as e:
            log.warning("Could not post suggestion guild=%s tournament=%s: %s", guild_id, tid, e)
            await database.delete_discovery_suggestion(guild_id, tid)  # let the next scan retry
            return
        await database.set_discovery_suggestion_message(guild_id, tid, msg.id)
        log.info("Suggested tournament %s to guild %s (%s)", tid, guild_id, suggestion.players)


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN not set.")
    MeleeBot().run(token)
