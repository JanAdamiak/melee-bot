import re
from typing import TYPE_CHECKING

import discord
from discord import app_commands, ui

from domain import db as database

if TYPE_CHECKING:
    from bot import MeleeBot


# ── suggestion buttons ─────────────────────────────────────────────────────────
# These are DynamicItems: the tournament ID travels in the button's custom_id,
# so a click is routed here even after a restart, with no view kept in memory.

async def _may_act(interaction: discord.Interaction) -> bool:
    if interaction.permissions.manage_guild:
        return True
    await interaction.response.send_message(
        "You need the **Manage Server** permission to act on tournament suggestions.",
        ephemeral=True,
    )
    return False


async def _pending_suggestion(interaction: discord.Interaction, tournament_id: int) -> dict | None:
    """The suggestion a button belongs to, if it is still waiting for an answer."""
    suggestion = await database.get_discovery_suggestion(interaction.guild_id, tournament_id)
    if suggestion and suggestion["status"] == database.SUGGESTION_PENDING:
        return suggestion
    await interaction.response.send_message(
        "This suggestion has already been dealt with.", ephemeral=True
    )
    return None


def _resolved_embed(message: discord.Message, footer: str) -> discord.Embed | None:
    if not message.embeds:
        return None
    embed = message.embeds[0]
    embed.color = discord.Color.greyple()
    embed.set_footer(text=footer)
    return embed


class BindSuggestionButton(ui.DynamicItem[ui.Button], template=r"discovery:bind:(?P<tid>\d+)"):
    def __init__(self, tournament_id: int) -> None:
        super().__init__(ui.Button(
            label="Bind & watch",
            style=discord.ButtonStyle.green,
            custom_id=f"discovery:bind:{tournament_id}",
        ))
        self.tournament_id = tournament_id

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: ui.Button, match: re.Match[str],
    ) -> "BindSuggestionButton":
        return cls(int(match["tid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: MeleeBot = interaction.client  # type: ignore[assignment]
        guild_id, tid = interaction.guild_id, self.tournament_id
        if not await _may_act(interaction):
            return
        suggestion = await _pending_suggestion(interaction, tid)
        if suggestion is None:
            return

        # Players may have left the roster since the suggestion was posted.
        roster = set(await database.list_players(guild_id))
        players = [p for p in suggestion["players"] if p in roster]
        if not players:
            await interaction.response.send_message(
                "None of the suggested players are on the roster any more.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        name = await bot.bind_tournament(guild_id, tid, str(interaction.user))
        existing = await database.get_tournament_players(guild_id, tid)
        await database.set_tournament_players(guild_id, tid, sorted(set(existing) | set(players)))
        await database.set_discovery_suggestion_status(guild_id, tid, database.SUGGESTION_BOUND)

        # Watch it in the discovery channel (normally the one this message is in).
        channel_id = await database.get_discovery_channel(guild_id)
        channel = bot.get_channel(channel_id) if channel_id else None
        outcome = await bot.watch_in_channel(guild_id, channel or interaction.channel, tid)

        await interaction.message.edit(
            embed=_resolved_embed(interaction.message, f"✅ Bound by {interaction.user}"), view=None
        )
        title = name or f"Tournament {tid}"
        await interaction.followup.send(f"Bound **{title}**. {outcome}", ephemeral=True)


class IgnoreSuggestionButton(ui.DynamicItem[ui.Button], template=r"discovery:ignore:(?P<tid>\d+)"):
    def __init__(self, tournament_id: int) -> None:
        super().__init__(ui.Button(
            label="Ignore",
            style=discord.ButtonStyle.grey,
            custom_id=f"discovery:ignore:{tournament_id}",
        ))
        self.tournament_id = tournament_id

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: ui.Button, match: re.Match[str],
    ) -> "IgnoreSuggestionButton":
        return cls(int(match["tid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await _may_act(interaction):
            return
        if await _pending_suggestion(interaction, self.tournament_id) is None:
            return
        await database.set_discovery_suggestion_status(
            interaction.guild_id, self.tournament_id, database.SUGGESTION_IGNORED
        )
        await interaction.response.edit_message(
            embed=_resolved_embed(interaction.message, f"Ignored by {interaction.user}"), view=None
        )


def suggestion_view(tournament_id: int) -> ui.View:
    view = ui.View(timeout=None)
    view.add_item(BindSuggestionButton(tournament_id))
    view.add_item(IgnoreSuggestionButton(tournament_id))
    return view


class DiscoveryGroup(app_commands.Group):
    def __init__(self, bot: "MeleeBot") -> None:
        super().__init__(
            name="discovery",
            description="Suggest tournaments your roster players have entered",
            guild_only=True,
            default_permissions=discord.Permissions(manage_guild=True),
        )
        self.bot = bot

    @app_commands.command(
        name="channel", description="Turn discovery on and choose where suggestions are posted"
    )
    @app_commands.describe(channel="Channel for suggestions (defaults to this one)")
    async def channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel | None = None,
    ) -> None:
        target = channel or interaction.channel
        await database.set_discovery_channel(interaction.guild_id, target.id)
        await interaction.response.send_message(
            f"Discovery is on. Tournament suggestions will be posted in <#{target.id}>.",
            ephemeral=True,
        )

    @app_commands.command(name="off", description="Turn discovery off for this server")
    async def off(self, interaction: discord.Interaction) -> None:
        was_on = await database.get_discovery_channel(interaction.guild_id) is not None
        await database.set_discovery_channel(interaction.guild_id, None)
        await interaction.response.send_message(
            "Discovery is off." if was_on else "Discovery was already off.", ephemeral=True
        )
