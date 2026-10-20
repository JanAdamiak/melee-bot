from typing import TYPE_CHECKING

import discord
from discord import app_commands

from commands.views import BindPlayerView, TournamentSettingsSelectView
from domain import db as database
from domain import tracker
from domain.tracker import format_players

if TYPE_CHECKING:
    from bot import MeleeBot


class TournamentGroup(app_commands.Group):
    def __init__(self, bot: "MeleeBot") -> None:
        super().__init__(
            name="tournament", description="Manage tournament–player bindings", guild_only=True
        )
        self.bot = bot

    @app_commands.command(name="bind", description="Bind players to a tournament")
    @app_commands.describe(
        tournament="Tournament ID or melee.gg URL",
        rounds_to_track=(
            "Number of rounds to track before concluding (Swiss + top cut). "
            "Leave empty to auto-detect the final round by name."
        ),
    )
    async def bind(
        self,
        interaction: discord.Interaction,
        tournament: str,
        rounds_to_track: int | None = None,
    ) -> None:
        try:
            tid = tracker.parse_tournament_id(tournament)
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        guild_id = interaction.guild_id
        players = await database.list_players(guild_id)
        if not players:
            await interaction.response.send_message(
                "No players in roster. Use `/player add` first.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)

        name = await self.bot.bind_tournament(guild_id, tid, str(interaction.user), rounds_to_track)
        existing = await database.get_tournament_players(guild_id, tid)
        display_names = await database.get_display_names(guild_id)
        view = BindPlayerView(self.bot, tid, players, existing, display_names)
        title = name or f"Tournament {tid}"
        await interaction.followup.send(
            f"Select players for [{title}](https://melee.gg/Tournament/View/{tid}):",
            view=view,
            ephemeral=True,
        )

    @app_commands.command(name="unbind", description="Remove a tournament and its player bindings")
    @app_commands.describe(tournament="Tournament ID or melee.gg URL")
    async def unbind(self, interaction: discord.Interaction, tournament: str) -> None:
        try:
            tid = tracker.parse_tournament_id(tournament)
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        removed = await database.remove_tournament(interaction.guild_id, tid)
        if removed:
            await interaction.response.send_message(
                f"Removed tournament **{tid}** and its player bindings.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"Tournament **{tid}** not found.", ephemeral=True
            )

    @app_commands.command(name="settings", description="Edit settings for a bound tournament")
    async def settings(self, interaction: discord.Interaction) -> None:
        tournaments = await database.list_tournaments(interaction.guild_id)
        if not tournaments:
            await interaction.response.send_message(
                "No tournaments bound. Use `/tournament bind` first.", ephemeral=True
            )
            return
        view = TournamentSettingsSelectView(self.bot, tournaments)
        await interaction.response.send_message(
            "Select a tournament to edit:", view=view, ephemeral=True
        )

    @app_commands.command(name="list", description="List all bound tournaments and their players")
    async def list_tournaments(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        tournaments = await database.list_tournaments(interaction.guild_id)
        if not tournaments:
            await interaction.followup.send(
                "No tournaments bound. Use `/tournament bind <id>` to add one.", ephemeral=True
            )
            return
        display_names = await self.bot.all_display_names(interaction.guild_id)
        lines = []
        for t in tournaments:
            status = "✅ concluded" if t["concluded"] else "🔄 active"
            players_str = format_players(t["players"], display_names) or "*(no players bound)*"
            rounds_str = (
                f" · tracking {t['rounds_to_track']} rounds" if t.get("rounds_to_track") else ""
            )
            title = t.get("name") or str(t["id"])
            header = f"**[{title}](https://melee.gg/Tournament/View/{t['id']})** {status}{rounds_str}"
            lines.append(f"{header}\n  {players_str}")
        await interaction.followup.send("\n\n".join(lines), ephemeral=True)
