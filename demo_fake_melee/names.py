"""Invented usernames to stand in for the real players in the fixtures."""

import hashlib
from collections.abc import Iterable

_ADJECTIVES = [
    "Amber", "Arctic", "Ashen", "Azure", "Bold", "Brass", "Bright", "Calm", "Cedar", "Cinder",
    "Clever", "Cobalt", "Copper", "Coral", "Crimson", "Dapper", "Dusty", "Eager", "Ember", "Fabled",
    "Feral", "Frosty", "Gentle", "Gilded", "Glassy", "Golden", "Hazel", "Hollow", "Indigo", "Iron",
    "Ivory", "Jade", "Jolly", "Keen", "Lucky", "Lunar", "Mellow", "Misty", "Nimble", "Noble",
    "Onyx", "Pale", "Plucky", "Quick", "Quiet", "Rusty", "Sable", "Scarlet", "Shady", "Silent",
    "Silver", "Sleepy", "Solar", "Stormy", "Sunny", "Swift", "Tidal", "Velvet", "Wild", "Wry",
]
_NOUNS = [
    "Badger", "Beacon", "Birch", "Bison", "Comet", "Condor", "Cricket", "Dingo", "Drake", "Falcon",
    "Fennec", "Ferret", "Finch", "Gecko", "Glacier", "Harbor", "Heron", "Ibex", "Jackal", "Kiwi",
    "Lantern", "Lark", "Lynx", "Magpie", "Marten", "Meadow", "Moth", "Newt", "Ocelot", "Orchid",
    "Osprey", "Otter", "Panda", "Pebble", "Pike", "Puffin", "Quail", "Raven", "Reef", "Robin",
    "Salmon", "Sparrow", "Spruce", "Stoat", "Summit", "Tapir", "Thistle", "Thrush", "Tiger", "Toad",
    "Tundra", "Viper", "Walrus", "Weasel", "Willow", "Wombat", "Wren", "Yak", "Zebra", "Zephyr",
]


def _candidate(real: str, attempt: int) -> str:
    digest = hashlib.sha256(f"{real}#{attempt}".encode()).digest()
    name = _ADJECTIVES[digest[0] % len(_ADJECTIVES)] + _NOUNS[digest[1] % len(_NOUNS)]
    # Later attempts add a number, so a crowded field still gets unique names.
    return name if attempt == 0 else f"{name}{digest[2] % 90 + 10}"


def build_aliases(real_usernames: Iterable[str], reserved: Iterable[str] = ()) -> dict[str, str]:
    """A stable invented username for each real one. The same input always
    gives the same output; nothing in `reserved` is ever handed out."""
    taken = {name.lower() for name in reserved}
    aliases: dict[str, str] = {}
    for real in sorted(set(real_usernames)):
        attempt = 0
        while (alias := _candidate(real, attempt)).lower() in taken:
            attempt += 1
        taken.add(alias.lower())
        aliases[real] = alias
    return aliases
