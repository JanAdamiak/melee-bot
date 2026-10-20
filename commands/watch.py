import datetime
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from commands.views import WatchSelectView
from domain import db as database
from domain.tracker import LONDON, build_embed, format_players, tally_str

if TYPE_CHECKING:
    from bot import MeleeBot


class WatchGroup(app_commands.Group):
    def __init__(self, bot: "MeleeBot") -> None:
        super().__init__(name="watch", description="Manage tournament watches", guild_only=True)
        self.bot = bot

    @app_commands.command(name="start", description="Start watching bound tournaments")
    async def start(self, interaction: discord.Interaction) -> None:
        tournaments = await database.list_tournaments(interaction.guild_id)
        active = [t for t in tournaments if not t["concluded"] and t["players"]]
        display_names = await database.get_display_names(interaction.guild_id)
        view = WatchSelectView(self.bot, active, display_names)
        message = (
            "Select tournaments to watch, or add a new one:"
            if active
            else "No active tournaments yet. Add one to start watching:"
        )
        await interaction.response.send_message(message, view=view, ephemeral=True)

    @app_commands.command(
        name="status",
        description="Show win/loss/draw summary for all active and concluded tournaments",
    )
    async def status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

        all_display_names = await self.bot.all_display_names(interaction.guild_id)

        lines: list[str] = []

        for session in self.bot.watches.values():
            if session.guild_id != interaction.guild_id:
                continue
            channel = self.bot.get_channel(session.channel_id)
            channel_tag = f" · #{channel.name}" if hasattr(channel, "name") else ""
            for tw in session.tournament_watches:
                lines.append(
                    f"**[{tw.title}](https://melee.gg/Tournament/View/{tw.tournament_id})**"
                    f"{channel_tag} {'🏁' if tw.concluded else '🔄'}"
                )
                if tw.snapshot.error:
                    lines.append(f"⚠️ {tw.snapshot.error}")
                    continue
                for player, ps in tw.snapshot.players.items():
                    display = all_display_names.get(player, player)
                    lines.append(f"`{display}` — {tally_str(ps, include_pending=True)}")

        tournaments = await database.list_tournaments(interaction.guild_id)
        concluded = [t for t in tournaments if t["concluded"]]
        if concluded:
            if lines:
                lines.append("")
            lines.append("**Concluded:**")
            for t in concluded:
                players_str = format_players(t["players"], all_display_names)
                title = t.get("name") or f"Tournament {t['id']}"
                lines.append(
                    f"[{title}](https://melee.gg/Tournament/View/{t['id']})"
                    + (f" — {players_str}" if players_str else "")
                )

        await interaction.followup.send("\n".join(lines) if lines else "No active or recent tournaments.")

    @app_commands.command(name="stop", description="Stop the active watch in this channel")
    async def stop(self, interaction: discord.Interaction) -> None:
        session = await self.bot.drop_watch(interaction.channel_id)
        if not session:
            await interaction.response.send_message(
                "No active watch in this channel.", ephemeral=True
            )
            return
        try:
            msg = await interaction.channel.fetch_message(session.message_id)
            now = datetime.datetime.now(LONDON).strftime("%H:%M %Z")
            embed = build_embed(session, [], concluded=True)
            embed.set_footer(text=f"Stopped by {interaction.user} · {now}")
            await msg.edit(embed=embed)
        except discord.NotFound:
            pass
        await interaction.response.send_message("Watch stopped.", ephemeral=True)
