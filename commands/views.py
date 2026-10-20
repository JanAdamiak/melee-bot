from typing import TYPE_CHECKING

import discord
from discord import ui

if TYPE_CHECKING:
    from bot import MeleeBot

MAX_TOURNAMENTS = 4  # 4 select rows + 1 button row = Discord's 5 ActionRow limit


def _tournament_title(t: dict) -> str:
    name = t.get("name") or f"Tournament {t['id']}"
    return name if len(name) <= 100 else name[:97] + "…"  # Discord SelectOption label cap


# ── /tournament bind ───────────────────────────────────────────────────────────

class BindPlayerSelect(ui.Select):
    def __init__(
        self,
        tournament_id: int,
        players: list[str],
        existing: list[str],
        display_names: dict[str, str] | None = None,
    ) -> None:
        dn = display_names or {}
        options = [
            discord.SelectOption(
                label=dn.get(p, p),
                value=p,
                description=p if p in dn else None,
                default=p in existing,
            )
            for p in players
        ]
        super().__init__(
            placeholder=f"Players for tournament {tournament_id}",
            min_values=1,
            max_values=len(players),
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: BindPlayerView = self.view  # type: ignore[assignment]
        view.selected = list(self.values)
        await interaction.response.defer()


class BindPlayerView(ui.View):
    def __init__(
        self,
        bot: "MeleeBot",
        tournament_id: int,
        players: list[str],
        existing: list[str],
        display_names: dict[str, str] | None = None,
    ) -> None:
        super().__init__(timeout=120)
        self.bot = bot
        self.tournament_id = tournament_id
        self.selected: list[str] = list(existing)
        self.add_item(BindPlayerSelect(tournament_id, players, existing, display_names))

    @ui.button(label="Save Binding", style=discord.ButtonStyle.green, row=1)
    async def save(self, interaction: discord.Interaction, button: ui.Button) -> None:
        if not self.selected:
            await interaction.response.send_message(
                "Select at least one player.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        await self.bot.db.set_tournament_players(
            interaction.guild_id, self.tournament_id, self.selected
        )
        players_str = ", ".join(self.selected)
        await interaction.followup.send(
            f"Saved **{len(self.selected)}** player(s) for tournament "
            f"**{self.tournament_id}**: {players_str}",
            ephemeral=True,
        )
        self.stop()


# ── /watch start ──────────────────────────────────────────────────────────────

class WatchTournamentSelect(ui.Select):
    def __init__(self, tournaments: list[dict], display_names: dict[str, str] | None = None) -> None:
        dn = display_names or {}
        options = [
            discord.SelectOption(
                label=_tournament_title(t),
                value=str(t["id"]),
                description=(
                    ", ".join(dn.get(p, p) for p in t["players"][:3])
                    + ("…" if len(t["players"]) > 3 else "")
                ),
            )
            for t in tournaments
        ]
        super().__init__(
            placeholder="Select tournaments to watch",
            min_values=1,
            max_values=min(len(tournaments), MAX_TOURNAMENTS),
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: WatchSelectView = self.view  # type: ignore[assignment]
        view.selected_ids = [int(v) for v in self.values]
        await interaction.response.defer()


class WatchSelectView(ui.View):
    def __init__(
        self,
        bot: "MeleeBot",
        tournaments: list[dict],
        display_names: dict[str, str] | None = None,
    ) -> None:
        super().__init__(timeout=120)
        self.bot = bot
        self.selected_ids: list[int] = []
        if tournaments:
            self.add_item(WatchTournamentSelect(tournaments, display_names))
        else:
            self.remove_item(self.start)

    @ui.button(label="Start Watching", style=discord.ButtonStyle.green, row=1)
    async def start(self, interaction: discord.Interaction, button: ui.Button) -> None:
        if not self.selected_ids:
            await interaction.response.send_message(
                "Select at least one tournament.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        await self.bot.start_watch(interaction, self.selected_ids)
        self.stop()

    @ui.button(label="Add New Tournament", style=discord.ButtonStyle.grey, row=1)
    async def add_new(self, interaction: discord.Interaction, button: ui.Button) -> None:
        await interaction.response.send_modal(TournamentModal(self.bot))
        self.stop()


# ── /watch start (new tournament) ─────────────────────────────────────────────

class TournamentModal(ui.Modal, title="Start Tournament Watch"):
    tournament_ids = ui.TextInput(
        label="Tournament IDs or URLs",
        placeholder="430189\nhttps://melee.gg/Tournament/View/430200",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=500,
    )

    def __init__(self, bot: "MeleeBot") -> None:
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from domain.tracker import parse_tournament_id

        lines = [line.strip() for line in self.tournament_ids.value.splitlines() if line.strip()]
        try:
            ids = [parse_tournament_id(line) for line in lines]
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return

        if not ids:
            await interaction.response.send_message("No tournament IDs provided.", ephemeral=True)
            return

        if len(ids) > MAX_TOURNAMENTS:
            await interaction.response.send_message(
                f"Maximum {MAX_TOURNAMENTS} tournaments per watch.", ephemeral=True
            )
            return

        players = await self.bot.db.list_players(interaction.guild_id)
        if not players:
            await interaction.response.send_message(
                "No players in the roster yet. Use `/player add` first.", ephemeral=True
            )
            return

        display_names = await self.bot.db.get_display_names(interaction.guild_id)
        view = PlayerSelectView(self.bot, ids, players, display_names)
        await interaction.response.send_message(
            "Select players for each tournament, then click **Start Watching**.",
            view=view,
            ephemeral=True,
        )


class TournamentPlayerSelect(ui.Select):
    def __init__(
        self,
        tournament_id: int,
        players: list[str],
        row: int,
        display_names: dict[str, str] | None = None,
    ) -> None:
        dn = display_names or {}
        options = [
            discord.SelectOption(
                label=dn.get(p, p),
                value=p,
                description=p if p in dn else None,
            )
            for p in players
        ]
        super().__init__(
            placeholder=f"Players for tournament {tournament_id}",
            min_values=1,
            max_values=len(players),
            options=options,
            row=row,
        )
        self.tournament_id = tournament_id

    async def callback(self, interaction: discord.Interaction) -> None:
        view: PlayerSelectView = self.view  # type: ignore[assignment]
        view.selections[self.tournament_id] = self.values
        await interaction.response.defer()


class PlayerSelectView(ui.View):
    def __init__(
        self,
        bot: "MeleeBot",
        tournament_ids: list[int],
        players: list[str],
        display_names: dict[str, str] | None = None,
    ) -> None:
        super().__init__(timeout=120)
        self.bot = bot
        self.tournament_ids = tournament_ids
        self.selections: dict[int, list[str]] = {tid: [] for tid in tournament_ids}

        for row, tid in enumerate(tournament_ids):
            self.add_item(TournamentPlayerSelect(tid, players, row=row, display_names=display_names))

    @ui.button(label="Start Watching", style=discord.ButtonStyle.green, row=4)
    async def start(self, interaction: discord.Interaction, button: ui.Button) -> None:
        missing = [tid for tid, sel in self.selections.items() if not sel]
        if missing:
            await interaction.response.send_message(
                f"Please select players for: {', '.join(str(t) for t in missing)}",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        # Persist bindings so they survive restarts and appear in /tournament list.
        for tid, players in self.selections.items():
            await self.bot.bind_tournament(interaction.guild_id, tid, str(interaction.user))
            await self.bot.db.set_tournament_players(interaction.guild_id, tid, players)

        await self.bot.start_watch(interaction, self.tournament_ids)
        self.stop()


# ── /tournament settings ───────────────────────────────────────────────────────

class TournamentSettingsModal(ui.Modal, title="Tournament Settings"):
    name_input = ui.TextInput(
        label="Tournament name",
        placeholder="Leave empty to keep the current name",
        required=False,
        max_length=100,
    )
    rounds_to_track = ui.TextInput(
        label="Rounds to track",
        placeholder="Leave empty to auto-detect the final round by name",
        required=False,
        max_length=4,
    )

    def __init__(
        self, bot: "MeleeBot", tournament_id: int, current_rounds: int | None, name: str | None = None
    ) -> None:
        super().__init__()
        self.bot = bot
        self.tournament_id = tournament_id
        self.name = name
        if name:
            self.name_input.default = name
        if current_rounds is not None:
            self.rounds_to_track.default = str(current_rounds)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.rounds_to_track.value.strip()
        rounds_to_track: int | None = None
        if raw:
            try:
                rounds_to_track = int(raw)
                if rounds_to_track <= 0:
                    raise ValueError
            except ValueError:
                await interaction.response.send_message(
                    "Rounds to track must be a positive whole number.", ephemeral=True
                )
                return

        new_name = self.name_input.value.strip() or self.name

        await self.bot.db.set_tournament_rounds_to_track(
            interaction.guild_id, self.tournament_id, rounds_to_track
        )
        if new_name != self.name:
            await self.bot.db.set_tournament_name(self.tournament_id, new_name)
            self.bot.update_tournament_name(self.tournament_id, new_name)

        title = new_name or f"Tournament {self.tournament_id}"
        if rounds_to_track is not None:
            msg = f"**{title}** will now track **{rounds_to_track}** rounds."
        else:
            msg = f"**{title}** will auto-detect the final round by name."
        await interaction.response.send_message(msg, ephemeral=True)


class TournamentSettingsSelect(ui.Select):
    def __init__(self, tournaments: list[dict]) -> None:
        self.tournaments = {t["id"]: t for t in tournaments}
        options = [
            discord.SelectOption(
                label=_tournament_title(t),
                value=str(t["id"]),
                description=(
                    f"tracking {t['rounds_to_track']} rounds"
                    if t.get("rounds_to_track")
                    else "auto-detect rounds"
                ),
            )
            for t in tournaments
        ]
        super().__init__(
            placeholder="Select a tournament",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: TournamentSettingsSelectView = self.view  # type: ignore[assignment]
        tid = int(self.values[0])
        t = self.tournaments[tid]
        await interaction.response.send_modal(
            TournamentSettingsModal(view.bot, tid, t["rounds_to_track"], t.get("name"))
        )
        view.stop()


class TournamentSettingsSelectView(ui.View):
    def __init__(self, bot: "MeleeBot", tournaments: list[dict]) -> None:
        super().__init__(timeout=120)
        self.bot = bot
        self.add_item(TournamentSettingsSelect(tournaments))


# ── /player add ────────────────────────────────────────────────────────────────

class PlayerAddModal(ui.Modal, title="Add Player(s)"):
    players_input = ui.TextInput(
        label="Players (one per line)",
        placeholder="shroomed\nhbox | Hungrybox",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=500,
    )

    def __init__(self, bot: "MeleeBot") -> None:
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        lines = [line.strip() for line in self.players_input.value.splitlines() if line.strip()]
        if not lines:
            await interaction.response.send_message("No players provided.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        added: list[str] = []
        skipped: list[str] = []
        for line in lines:
            name, _, dn = line.partition("|")
            name, dn = name.strip(), dn.strip() or None
            ok = await self.bot.db.add_player(interaction.guild_id, name, str(interaction.user), dn)
            (added if ok else skipped).append(f"{name} (shown as **{dn}**)" if ok and dn else name)

        parts = []
        if added:
            parts.append(f"Added: {', '.join(added)}")
        if skipped:
            parts.append(f"Already in roster: {', '.join(skipped)}")
        await interaction.followup.send("\n".join(parts), ephemeral=True)
