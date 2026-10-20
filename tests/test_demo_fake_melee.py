"""The fake melee.gg in demo_fake_melee/, checked through the bot's own client."""

import datetime
import json
import threading

import pytest

from demo_fake_melee import scenario
from demo_fake_melee.server import make_server
from demo_fake_melee.world import FIXTURES, SWISS_ROUNDS, World, _load_rounds, compute_standings
from domain import db, topcut, tracker
from melee_tiebreakers import melee_client

NORTHGATE, MIDSUMMER, HARBOUR = (spec["id"] for spec in scenario.TOURNAMENTS)


@pytest.fixture
def world(tmp_path):
    return World(state_path=tmp_path / "state.json")


@pytest.fixture
def site(world, monkeypatch):
    """The fake site on a free local port, with the client pointed at it."""
    server = make_server(world, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(melee_client, "BASE_URL", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setattr(melee_client, "_CRAWL_DELAY_SECONDS", 0.0)
    yield world
    server.shutdown()
    server.server_close()


def _real_names() -> set[str]:
    names = set()
    for spec in scenario.TOURNAMENTS:
        for rows in _load_rounds(spec["fixture"]):
            for row in rows:
                for comp in row["Competitors"]:
                    for player in comp["Team"]["Players"]:
                        names.update((player["Username"], player["DisplayName"]))
    return names


@pytest.mark.parametrize("fixture", [spec["fixture"] for spec in scenario.TOURNAMENTS])
def test_computed_standings_match_the_captured_ones(fixture):
    computed = compute_standings(_load_rounds(fixture))
    captured = json.loads((FIXTURES / f"tournament_{fixture}" / "standings_round_12.json").read_text())
    for real in captured:
        mine = computed[real["Rank"] - 1]
        assert mine["TeamId"] == real["TeamId"]
        assert mine["MatchRecord"] == real["MatchRecord"]
        assert mine["GameRecord"] == real["GameRecord"]
        assert mine["Points"] == real["Points"]
        for pct in ("OpponentMatchWinPercentage", "TeamGameWinPercentage", "OpponentGameWinPercentage"):
            assert mine[pct] == pytest.approx(real[pct], abs=1e-6)


def test_no_real_player_name_is_served(world):
    real = _real_names()
    for t in world.tournaments.values():
        assert not t.usernames & real
        for rows in t.rounds:
            for row in rows:
                names = [p[key] for c in row["Competitors"] for p in c["Team"]["Players"]
                         for key in ("Username", "DisplayName")]
                assert not set(names) & real
                result = row["ResultString"]
                assert result.endswith("Draw") or result.startswith(tuple(n + " " for n in names))


def test_every_demo_player_is_in_every_tournament(world):
    cast = {username for roster in scenario.ROSTERS for username, _ in roster}
    for t in world.tournaments.values():
        assert cast <= t.usernames


async def test_tournament_page_reflects_the_position(site):
    page = await melee_client.get_tournament_page(MIDSUMMER)
    assert page.name == "Midsummer Invitational"
    assert page.start_time > datetime.datetime.now(datetime.UTC)
    assert not page.ended
    assert len(page.rounds) == SWISS_ROUNDS
    assert not any(r["is_started"] for r in page.rounds)

    site.goto(MIDSUMMER, 3, "half")
    page = await melee_client.get_tournament_page(MIDSUMMER)
    assert [r["is_started"] for r in page.rounds] == [True] * 3 + [False] * 9
    assert page.start_time <= datetime.datetime.now(datetime.UTC)

    site.end(MIDSUMMER)
    assert (await melee_client.get_tournament_page(MIDSUMMER)).ended


async def test_a_round_fills_in_step_by_step(site):
    site.goto(NORTHGATE, 4, "paired")
    round_id = site.tournaments[NORTHGATE].round_id(4)
    reported = []
    for _ in range(3):
        rows = await melee_client.get_matches(round_id, NORTHGATE)
        played = [r for r in rows if len(r["Competitors"]) == 2]
        reported.append(sum(r["HasResult"] for r in played))
        assert all((r["ResultString"] is not None) == r["HasResult"] for r in played)
        site.advance(NORTHGATE)
    assert reported[0] == 0 < reported[1] < reported[2] == len(played)
    # The third advance paired round 5.
    assert site.tournaments[NORTHGATE].position.startswith("Round 5")


async def test_watch_snapshot_and_changes(site):
    players = [username for username, _ in scenario.ROSTERS[0]]
    before = await tracker.fetch_snapshot(NORTHGATE, players)
    assert before.name == "Northgate Open" and before.rounds_started == 2
    assert all(ps.rank is not None and len(ps.rounds) == 2 for ps in before.players.values())

    for _ in range(3):   # round 3: paired, half, done
        site.advance(NORTHGATE)
    after = await tracker.fetch_snapshot(NORTHGATE, players)
    changes = tracker.diff(before, after)
    assert {c.player for c in changes if "Round 3 vs" in c.description} == set(players)


async def test_not_started_and_unknown_tournaments(site):
    snap = await tracker.fetch_snapshot(MIDSUMMER, ["Kestrel_VX"])
    assert snap.not_started
    with pytest.raises(Exception):
        await melee_client.get_tournament_page(123)


async def test_topcut_has_what_it_needs(site):
    await db.init_db()   # /topcut caches finished rounds in the database
    data = await topcut.fetch_tournament_data(HARBOUR)
    assert data.current_round["name"] == "Round 11" and data.project_next_round
    assert data.baseline_standings and data.current_matches
    assert not any(m.reported for m in data.current_matches if not m.is_bye)


async def test_profile_lists_a_players_tournaments(site):
    found = await melee_client.get_player_tournaments("QuietRook")
    assert [t.tournament_id for t in found] == [HARBOUR, NORTHGATE, MIDSUMMER]
    assert not any(t.ended for t in found)
    assert await melee_client.get_player_tournaments("nobody_by_this_name") == []


def test_positions_survive_a_restart(tmp_path):
    first = World(state_path=tmp_path / "state.json")
    first.goto(MIDSUMMER, 6, "half")
    first.end(NORTHGATE)
    second = World(state_path=tmp_path / "state.json")
    assert second.tournaments[MIDSUMMER].position == first.tournaments[MIDSUMMER].position
    assert second.tournaments[NORTHGATE].ended
    second.reset()
    assert second.tournaments[MIDSUMMER].step == 0 and not second.tournaments[NORTHGATE].ended


def test_advancing_past_the_last_round_ends_the_tournament(world):
    world.goto(HARBOUR, SWISS_ROUNDS, "done")
    assert not world.tournaments[HARBOUR].ended
    world.advance(HARBOUR)
    assert world.tournaments[HARBOUR].ended


def test_demo_launcher_shortens_the_bots_waits():
    from demo_fake_melee import run_bot
    from domain import feed

    originals = (feed.ACTIVE_POLL, feed.IDLE_POLL, feed.IDLE_AFTER, feed.FRESH_EMBED_AFTER,
                 topcut.NORMAL_CADENCE_SECONDS)
    try:
        bot_class = run_bot.shorten_waits()
        assert feed.IDLE_AFTER == run_bot.IDLE_AFTER < originals[2]
        assert bot_class.poll_task.seconds == run_bot.POLL_TICK_SECONDS
        assert bot_class.discovery_task.minutes == run_bot.DISCOVERY_SCAN_MINUTES
    finally:
        (feed.ACTIVE_POLL, feed.IDLE_POLL, feed.IDLE_AFTER, feed.FRESH_EMBED_AFTER,
         topcut.NORMAL_CADENCE_SECONDS) = originals
        bot_class.poll_task.change_interval(seconds=60)
        bot_class.discovery_task.change_interval(hours=3)


def test_demo_env_refuses_the_real_site(tmp_path):
    from demo_fake_melee.env import load_demo_env

    env = tmp_path / ".env.demo"
    env.write_text("DISCORD_TOKEN=x\nGUILD_IDS=1,2\nDB_PATH=demo.db\nMELEE_BASE_URL=https://melee.gg\n")
    with pytest.raises(SystemExit, match="must point at this machine"):
        load_demo_env(env)
    env.write_text("GUILD_IDS=1,2\nDB_PATH=demo.db\nMELEE_BASE_URL=http://127.0.0.1:8765\n")
    with pytest.raises(SystemExit, match="DISCORD_TOKEN"):
        load_demo_env(env)
