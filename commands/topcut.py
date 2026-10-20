from typing import TYPE_CHECKING

import discord
from discord import app_commands

from domain import db as database
from domain import topcut, tracker
from scheduler import PRIORITY_TOPCUT_NORMAL

if TYPE_CHECKING:
    from bot import MeleeBot


class TopCutGroup(app_commands.Group):
    def __init__(self, bot: "MeleeBot") -> None:
        super().__init__(
            name="topcut", description="Exact top-cut make/miss tracking", guild_only=True
        )
        self.bot = bot

    @app_commands.command(name="watch", description="Track roster players' top-cut chances for a tournament")
    @app_commands.describe(tournament="Tournament ID or melee.gg URL")
    @app_commands.choices(cut=[
        app_commands.Choice(name="Top 8", value=8),
        app_commands.Choice(name="Top 16", value=16),
        app_commands.Choice(name="Top 32", value=32),
    ])
    async def watch(
        self,
        interaction: discord.Interaction,
        tournament: str,
        cut: app_commands.Choice[int],
    ) -> None:
        channel_id = interaction.channel_id
        if channel_id in self.bot.topcut_sessions:
            await interaction.response.send_message(
                "A /topcut watch is already active in this channel. Use `/topcut stop` first.",
                ephemeral=True,
            )
            return
        try:
            tid = tracker.parse_tournament_id(tournament)
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        name = await database.get_tournament_name(tid)
        if name is None:
            name = await tracker.get_tournament_name(tid)

        session = topcut.TopCutSession(
            guild_id=interaction.guild_id, channel_id=channel_id, message_id=0, tournament_id=tid,
            cut_size=cut.value, started_by=str(interaction.user), tournament_name=name,
        )
        await self.bot.scheduler.submit(PRIORITY_TOPCUT_NORMAL, lambda: topcut.refresh_session(session))
        if session.error and not session.results:
            await interaction.followup.send(f"⚠️ {session.error}", ephemeral=True)
            return

        self.bot.scheduler.set_bubble(tid, session.bubble)
        display_names = await self.bot.all_display_names(interaction.guild_id)
        embed = topcut.build_topcut_embed(session, display_names)
        msg = await interaction.channel.send(embed=embed)
        session.message_id = msg.id
        self.bot.topcut_sessions[channel_id] = session
        await database.save_topcut_session(
            interaction.guild_id, channel_id, msg.id, tid, cut.value, str(interaction.user)
        )
        await interaction.followup.send("Top-cut watch started!", ephemeral=True)

    @app_commands.command(name="stop", description="Stop the active /topcut watch in this channel")
    async def stop(self, interaction: discord.Interaction) -> None:
        session = self.bot.topcut_sessions.pop(interaction.channel_id, None)
        if not session:
            await interaction.response.send_message(
                "No active /topcut watch in this channel.", ephemeral=True
            )
            return
        self.bot.scheduler.set_bubble(session.tournament_id, False)
        await database.delete_topcut_session(interaction.channel_id)
        try:
            msg = await interaction.channel.fetch_message(session.message_id)
            embed = topcut.build_topcut_embed(session, await self.bot.all_display_names(interaction.guild_id))
            embed.set_footer(text=f"Stopped by {interaction.user}")
            await msg.edit(embed=embed)
        except discord.NotFound:
            pass
        await interaction.response.send_message("Top-cut watch stopped.", ephemeral=True)

    @app_commands.command(
        name="status", description="Show the current /topcut result without waiting for the next poll"
    )
    async def status(self, interaction: discord.Interaction) -> None:
        session = self.bot.topcut_sessions.get(interaction.channel_id)
        if not session:
            await interaction.response.send_message(
                "No active /topcut watch in this channel.", ephemeral=True
            )
            return
        display_names = await self.bot.all_display_names(interaction.guild_id)
        embed = topcut.build_topcut_embed(session, display_names)
        await interaction.response.send_message(embed=embed, ephemeral=True)
