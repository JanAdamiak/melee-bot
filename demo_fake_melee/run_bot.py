"""Run the bot against the fake site, with its waits shortened for a demo.

    uv run python -m demo_fake_melee.run_bot
"""

import datetime
import os

from demo_fake_melee.env import load_demo_env

# How long things take in the demo; the real values are in the comments.
ACTIVE_POLL = datetime.timedelta(seconds=15)          # 60 seconds
IDLE_POLL = datetime.timedelta(seconds=30)            # 15 minutes
IDLE_AFTER = datetime.timedelta(minutes=3)            # 90 minutes
FRESH_EMBED_AFTER = datetime.timedelta(minutes=10)    # 6 hours
TOPCUT_CADENCE_SECONDS = 15                           # 60 seconds
POLL_TICK_SECONDS = 10                                # 60 seconds
DISCOVERY_SCAN_MINUTES = 2                            # 3 hours


def shorten_waits() -> type:
    """Apply the demo timings and return the bot class, ready to run."""
    import bot
    from domain import feed, topcut

    feed.ACTIVE_POLL = ACTIVE_POLL
    feed.IDLE_POLL = IDLE_POLL
    feed.IDLE_AFTER = IDLE_AFTER
    feed.FRESH_EMBED_AFTER = FRESH_EMBED_AFTER
    topcut.NORMAL_CADENCE_SECONDS = TOPCUT_CADENCE_SECONDS
    bot.MeleeBot.poll_task.change_interval(seconds=POLL_TICK_SECONDS)
    bot.MeleeBot.discovery_task.change_interval(minutes=DISCOVERY_SCAN_MINUTES)
    return bot.MeleeBot


def main() -> None:
    load_demo_env()
    shorten_waits()().run(os.environ["DISCORD_TOKEN"])


if __name__ == "__main__":
    main()
