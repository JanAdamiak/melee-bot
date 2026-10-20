"""Loads .env.demo, so the demo can never run on the real bot's settings."""

import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

ENV_FILE = Path(".env.demo")
REQUIRED = ("DISCORD_TOKEN", "GUILD_IDS", "DB_PATH", "MELEE_BASE_URL")
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def load_demo_env(path: Path = ENV_FILE) -> None:
    """Must run before anything from the bot is imported: the database path
    and the melee.gg address are both read at import time."""
    if not path.exists():
        sys.exit(f"{path} not found. Copy demo_fake_melee/env.demo.example to {path} and fill it in.")
    # All four must come from the file itself: a DISCORD_TOKEN or DB_PATH
    # already in the shell, or picked up later from .env, would be the real bot's.
    values = {key: value for key, value in dotenv_values(path).items() if value}
    missing = [name for name in REQUIRED if name not in values]
    if missing:
        sys.exit(f"{path} must set {', '.join(missing)}.")
    host = urlsplit(values["MELEE_BASE_URL"]).hostname
    if host not in _LOCAL_HOSTS:
        sys.exit(f"MELEE_BASE_URL in {path} must point at this machine, not {host!r}.")
    os.environ.update(values)
