"""Fill the demo servers' rosters, to skip typing them in with /player add.

    uv run python -m demo_fake_melee.seed
"""

import asyncio

from demo_fake_melee import scenario
from demo_fake_melee.env import load_demo_env


async def seed() -> None:
    import config
    from domain import db

    await db.init_db(legacy_guild_id=config.legacy_guild_id())
    for guild_id, roster in zip(config.guild_ids(), scenario.ROSTERS):
        for username, display_name in roster:
            added = await db.add_player(guild_id, username, "demo seed", display_name)
            print(f"server {guild_id}: {username} {'added' if added else 'already on the roster'}")


if __name__ == "__main__":
    load_demo_env()
    asyncio.run(seed())
