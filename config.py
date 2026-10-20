"""Environment-derived settings that need parsing or validation."""

import os
from collections.abc import Mapping


def _parse_id(raw: str, var: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{var} must contain numeric Discord server IDs, got {raw!r}") from None
    if value <= 0:
        raise ValueError(f"{var} must contain numeric Discord server IDs, got {raw!r}")
    return value


def guild_ids(environ: Mapping[str, str] = os.environ) -> list[int]:
    """Servers to register commands in: GUILD_IDS=123,456, falling back to
    the older single GUILD_ID. Order is kept and duplicates are dropped."""
    raw = environ.get("GUILD_IDS", "").strip()
    if raw:
        ids = [_parse_id(part.strip(), "GUILD_IDS") for part in raw.split(",") if part.strip()]
    else:
        single = environ.get("GUILD_ID", "").strip()
        ids = [_parse_id(single, "GUILD_ID")] if single else []
    return list(dict.fromkeys(ids))


def legacy_guild_id(environ: Mapping[str, str] = os.environ) -> int | None:
    """The server that data from the old single-server database belongs to:
    GUILD_ID if it is still set, otherwise the first entry of GUILD_IDS."""
    single = environ.get("GUILD_ID", "").strip()
    if single:
        return _parse_id(single, "GUILD_ID")
    ids = guild_ids(environ)
    return ids[0] if ids else None
