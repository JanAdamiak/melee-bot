import discord

from domain.tracker import (
    Change,
    PlayerSnapshot,
    TournamentSnapshot,
    TournamentWatch,
    WatchSession,
    _count_results,
    _short_round,
    build_embed,
)


def _make_session(
    tournament_id: int = 430189,
    players: list[str] | None = None,
    rounds: dict | None = None,
    rank: int | None = 5,
) -> WatchSession:
    players = players or ["Alice"]
    rounds = rounds or {"Round 1": "2-0", "Round 2": "Not reported"}
    snap = TournamentSnapshot(
        tournament_id=tournament_id,
        players={
            p: PlayerSnapshot(
                rounds=rounds,
                opponents={r: "Opponent" for r in rounds},
                rank=rank,
                record="1-0",
                points=3,
            )
            for p in players
        },
    )
    tw = TournamentWatch(tournament_id, players, snap)
    return WatchSession(guild_id=1, channel_id=123, message_id=456, tournament_watches=[tw])


# ── color ──────────────────────────────────────────────────────────────────────

def test_embed_color_no_changes():
    embed = build_embed(_make_session(), [])
    assert embed.color == discord.Color.blue()

def test_embed_color_with_changes():
    embed = build_embed(_make_session(), [Change("Alice", "R1 vs Bob: **2-0**", True)])
    assert embed.color == discord.Color.gold()

def test_embed_color_concluded():
    embed = build_embed(_make_session(), [], concluded=True)
    assert embed.color == discord.Color.greyple()


# ── title & footer ─────────────────────────────────────────────────────────────

def test_embed_title():
    embed = build_embed(_make_session(), [])
    assert "melee.gg" in embed.title

def test_embed_footer_active():
    embed = build_embed(_make_session(), [])
    assert "/watch stop" in embed.footer.text

def test_embed_footer_concluded():
    embed = build_embed(_make_session(), [], concluded=True)
    assert "concluded" in embed.footer.text.lower()


# ── fields ─────────────────────────────────────────────────────────────────────

def test_embed_contains_player_name():
    embed = build_embed(_make_session(players=["mossgoblin"]), [])
    all_values = " ".join(f.value for f in embed.fields if f.value)
    assert "mossgoblin" in all_values

def test_embed_contains_tournament_link():
    embed = build_embed(_make_session(tournament_id=430189), [])
    all_names = " ".join(f.name for f in embed.fields if f.name)
    assert "430189" in all_names

def test_embed_changes_field_present():
    changes = [Change("Alice", "Round 1 vs Bob: **2-0**", True)]
    embed = build_embed(_make_session(), changes)
    assert any("Change" in (f.name or "") for f in embed.fields)

def test_embed_no_changes_field_when_empty():
    embed = build_embed(_make_session(), [])
    assert not any("Change" in (f.name or "") for f in embed.fields)

def test_embed_change_green_on_positive():
    changes = [Change("Alice", "won something", True)]
    embed = build_embed(_make_session(), changes)
    change_field = next(f for f in embed.fields if "Change" in (f.name or ""))
    assert "🟢" in change_field.value

def test_embed_change_red_on_negative():
    changes = [Change("Alice", "lost something", False)]
    embed = build_embed(_make_session(), changes)
    change_field = next(f for f in embed.fields if "Change" in (f.name or ""))
    assert "🔴" in change_field.value

def test_embed_change_neutral():
    changes = [Change("Alice", "got paired", None)]
    embed = build_embed(_make_session(), changes)
    change_field = next(f for f in embed.fields if "Change" in (f.name or ""))
    assert "⚪" in change_field.value


# ── result emoji in round rows ─────────────────────────────────────────────────

def test_embed_win_emoji():
    embed = build_embed(_make_session(rounds={"Round 1": "2-0"}), [])
    all_values = " ".join(f.value for f in embed.fields if f.value)
    assert "✅" in all_values

def test_embed_loss_emoji():
    embed = build_embed(_make_session(rounds={"Round 1": "0-2"}), [])
    all_values = " ".join(f.value for f in embed.fields if f.value)
    assert "❌" in all_values

def test_embed_pending_emoji():
    embed = build_embed(_make_session(rounds={"Round 1": "Not reported"}), [])
    all_values = " ".join(f.value for f in embed.fields if f.value)
    assert "⏳" in all_values


# ── multiple tournaments ───────────────────────────────────────────────────────

# ── _short_round ───────────────────────────────────────────────────────────────

def test_short_round_standard():
    assert _short_round("Round 1") == "R1"

def test_short_round_double_digit():
    assert _short_round("Round 14") == "R14"

def test_short_round_case_insensitive():
    assert _short_round("round 3") == "R3"

def test_short_round_non_standard_unchanged():
    assert _short_round("Quarterfinals") == "Quarterfinals"

def test_short_round_swiss_unchanged():
    assert _short_round("Top 8") == "Top 8"


# ── _count_results ─────────────────────────────────────────────────────────────

def _ps(rounds: dict[str, str], opponents: dict[str, str] | None = None) -> PlayerSnapshot:
    return PlayerSnapshot(rounds=rounds, opponents=opponents or {r: "Opp" for r in rounds})

def test_count_results_all_wins():
    assert _count_results(_ps({"R1": "2-0", "R2": "2-1"})) == (2, 0, 0)

def test_count_results_mixed():
    assert _count_results(_ps({"R1": "2-0", "R2": "0-2", "R3": "1-1"})) == (1, 1, 1)

def test_count_results_pending_excluded():
    w, losses, d = _count_results(_ps({"R1": "2-0", "R2": "Not reported"}))
    assert (w, losses, d) == (1, 0, 0)

def test_count_results_all_pending():
    assert _count_results(_ps({"R1": "Not reported"})) == (0, 0, 0)

def test_count_results_empty():
    assert _count_results(PlayerSnapshot()) == (0, 0, 0)

def test_count_results_bye_is_win():
    ps = _ps({"R1": "Alice was awarded a bye"})
    w, losses, d = _count_results(ps)
    assert w == 1 and losses == 0

def test_count_results_draw_format():
    ps = _ps({"R1": "1-1-0 Draw"})
    assert _count_results(ps) == (0, 0, 1)

def test_count_results_melee_win_format():
    ps = PlayerSnapshot(
        rounds={"R1": "Alice won 2-0-0"},
        opponents={"R1": "Bob"},
    )
    assert _count_results(ps) == (1, 0, 0)

def test_count_results_melee_loss_format():
    ps = PlayerSnapshot(
        rounds={"R1": "Bob won 2-0-0"},
        opponents={"R1": "Bob"},
    )
    assert _count_results(ps) == (0, 1, 0)


def test_count_results_team_loss_second_player():
    # Opponent stored as "EmberOsprey / Yael Millbrook"; winner is Yael Millbrook
    ps = PlayerSnapshot(
        rounds={"R1": "Yael Millbrook won 2-1-0"},
        opponents={"R1": "EmberOsprey / Yael Millbrook"},
    )
    assert _count_results(ps) == (0, 1, 0)


def test_count_results_team_win_first_player():
    # Tracked player's team wins; neither opponent name matches the winner
    ps = PlayerSnapshot(
        rounds={"R1": "QuietRook won 2-0-0"},
        opponents={"R1": "EmberOsprey / Yael Millbrook"},
    )
    assert _count_results(ps) == (1, 0, 0)


def test_count_results_display_name_in_result_string():
    # Opponent username "MistyComet", ResultString uses their display name "Yael Millbrook".
    # Tracker stores opponent as "MistyComet / Yael Millbrook" so the loss is detected.
    ps = PlayerSnapshot(
        rounds={"R1": "Yael Millbrook won 2-1-0"},
        opponents={"R1": "MistyComet / Yael Millbrook"},
    )
    assert _count_results(ps) == (0, 1, 0)


# ── dropped indicator ─────────────────────────────────────────────────────────

def test_embed_dropped_shown_when_two_fewer_rounds():
    snap = TournamentSnapshot(tournament_id=1, players={
        "Alice": PlayerSnapshot(rounds={f"R{i}": "2-0" for i in range(1, 9)}),  # 8 rounds
        "Bob":   PlayerSnapshot(rounds={f"R{i}": "2-0" for i in range(1, 7)}),  # 6 rounds → dropped
    })
    session = WatchSession(
        guild_id=1,
        channel_id=1, message_id=2, tournament_watches=[TournamentWatch(1, ["Alice", "Bob"], snap)]
    )
    embed = build_embed(session, [])
    all_values = " ".join(f.value for f in embed.fields if f.value)
    assert "dropped" in all_values

def test_embed_dropped_not_shown_when_equal_rounds():
    snap = TournamentSnapshot(tournament_id=1, players={
        "Alice": PlayerSnapshot(rounds={"R1": "2-0", "R2": "2-0"}),
        "Bob":   PlayerSnapshot(rounds={"R1": "2-0", "R2": "0-2"}),
    })
    session = WatchSession(
        guild_id=1,
        channel_id=1, message_id=2, tournament_watches=[TournamentWatch(1, ["Alice", "Bob"], snap)]
    )
    embed = build_embed(session, [])
    all_values = " ".join(f.value for f in embed.fields if f.value)
    assert "dropped" not in all_values

def test_embed_dropped_not_shown_when_one_fewer_round():
    snap = TournamentSnapshot(tournament_id=1, players={
        "Alice": PlayerSnapshot(rounds={"R1": "2-0", "R2": "2-0"}),
        "Bob":   PlayerSnapshot(rounds={"R1": "2-0"}),  # 1 fewer — not dropped
    })
    session = WatchSession(
        guild_id=1,
        channel_id=1, message_id=2, tournament_watches=[TournamentWatch(1, ["Alice", "Bob"], snap)]
    )
    embed = build_embed(session, [])
    all_values = " ".join(f.value for f in embed.fields if f.value)
    assert "dropped" not in all_values


# ── multiple tournaments ───────────────────────────────────────────────────────

def test_embed_multiple_tournaments():
    snap1 = TournamentSnapshot(tournament_id=111, players={
        "Alice": PlayerSnapshot(rounds={"Round 1": "2-0"})
    })
    snap2 = TournamentSnapshot(tournament_id=222, players={
        "Bob": PlayerSnapshot(rounds={"Round 1": "1-2"})
    })
    session = WatchSession(
        guild_id=1,
        channel_id=1,
        message_id=2,
        tournament_watches=[
            TournamentWatch(111, ["Alice"], snap1),
            TournamentWatch(222, ["Bob"], snap2),
        ],
    )
    embed = build_embed(session, [])
    field_names = " ".join(f.name for f in embed.fields if f.name)
    assert "111" in field_names
    assert "222" in field_names


# ── concluded tournaments inside a still-running watch ────────────────────────

def test_embed_marks_a_concluded_tournament_while_the_watch_goes_on():
    session = _make_session()
    session.tournament_watches[0].concluded = "ended"
    embed = build_embed(session, [])
    assert "🏁 concluded" in embed.fields[0].name


def test_embed_final_state_uses_the_footer_not_per_tournament_marks():
    session = _make_session()
    session.tournament_watches[0].concluded = "ended"
    embed = build_embed(session, [], concluded=True)
    assert "🏁" not in embed.fields[0].name
    assert "concluded" in embed.footer.text


# ── idle and not-started ───────────────────────────────────────────────────────

def test_embed_footer_while_idle():
    session = _make_session()
    session.idle = True
    assert "💤 Waiting for the next round" in build_embed(session, []).footer.text


def test_embed_footer_concluded_wins_over_idle():
    session = _make_session()
    session.idle = True
    assert "💤" not in build_embed(session, [], concluded=True).footer.text


def _not_started_session(start_time=None) -> WatchSession:
    snap = TournamentSnapshot(
        1, error="No rounds have started yet.", not_started=True, start_time=start_time
    )
    return WatchSession(
        guild_id=1, channel_id=1, message_id=2, tournament_watches=[TournamentWatch(1, ["Alice"], snap)]
    )


def test_embed_not_started_is_not_shown_as_an_error():
    value = build_embed(_not_started_session(), []).fields[0].value
    assert value == "⏳ Hasn't started yet"
    assert "⚠️" not in value


def test_embed_not_started_shows_relative_start_time():
    import datetime
    start = datetime.datetime(2026, 10, 3, 9, 0, tzinfo=datetime.UTC)
    value = build_embed(_not_started_session(start), []).fields[0].value
    assert f"<t:{int(start.timestamp())}:R>" in value
