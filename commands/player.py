from typing import TYPE_CHECKING

import discord
from discord import app_commands

from commands.views import PlayerAddModal
from domain import db as database

if TYPE_CHECKING:
    from bot import MeleeBot


async def player_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    players = await database.list_players(interaction.guild_id)
    return [
        app_commands.Choice(name=p, value=p)
        for p in players if current.lower() in p.lower()
    ][:25]


class PlayerGroup(app_commands.Group):
    def __init__(self, bot: "MeleeBot") -> None:
        super().__init__(
            name="player", description="Manage the tracked player roster", guild_only=True
        )
        self.bot = bot

    @app_commands.command(name="add", description="Add one or more melee.gg players to the roster")
    @app_commands.describe(
        name="melee.gg username (leave blank to add multiple players at once)",
        displayed_name="Friendly name shown in the tracker (optional)",
    )
    async def add(
        self,
        interaction: discord.Interaction,
        name: str | None = None,
        displayed_name: str | None = None,
    ) -> None:
        if name is None:
            await interaction.response.send_modal(PlayerAddModal(self.bot))
            return

        await interaction.response.defer(ephemeral=True)
        dn = displayed_name.strip() if displayed_name else None
        added = await database.add_player(
            interaction.guild_id, name.strip(), str(interaction.user), dn
        )
        if added:
            msg = f"Added: {name.strip()}" + (f" (shown as **{dn}**)" if dn else "")
        else:
            msg = f"Already in roster: {name.strip()}"
        await interaction.followup.send(msg, ephemeral=True)

    @app_commands.command(name="remove", description="Remove a player from the roster")
    @app_commands.describe(name="melee.gg username")
    @app_commands.autocomplete(name=player_autocomplete)
    async def remove(self, interaction: discord.Interaction, name: str) -> None:
        await interaction.response.defer(ephemeral=True)
        removed = await database.remove_player(interaction.guild_id, name.strip())
        if removed:
            await interaction.followup.send(f"Removed **{name}** from the roster.", ephemeral=True)
        else:
            await interaction.followup.send(f"**{name}** not found in the roster.", ephemeral=True)

    @app_commands.command(
        name="display-name",
        description="Set or update the friendly display name for a player",
    )
    @app_commands.describe(
        name="melee.gg username",
        displayed_name="Friendly name shown in the tracker (leave empty to clear)",
    )
    @app_commands.autocomplete(name=player_autocomplete)
    async def set_display_name(
        self,
        interaction: discord.Interaction,
        name: str,
        displayed_name: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        dn = displayed_name.strip() if displayed_name else None
        updated = await database.set_player_display_name(interaction.guild_id, name.strip(), dn)
        if updated:
            if dn:
                await interaction.followup.send(f"**{name}** will now be shown as **{dn}**.", ephemeral=True)
            else:
                await interaction.followup.send(f"Display name cleared for **{name}**.", ephemeral=True)
        else:
            await interaction.followup.send(f"**{name}** not found in the roster.", ephemeral=True)

    @app_commands.command(name="list", description="List all players in the roster")
    async def list_players(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        players = await database.list_players(interaction.guild_id)
        if not players:
            await interaction.followup.send(
                "Roster is empty. Use `/player add` to add players.", ephemeral=True
            )
            return
        display_names = await database.get_display_names(interaction.guild_id)
        lines = []
        for p in players:
            dn = display_names.get(p)
            lines.append(f"• {p}" + (f" → **{dn}**" if dn else ""))
        await interaction.followup.send(
            "**Roster:**\n" + "\n".join(lines),
            ephemeral=True,
        )
