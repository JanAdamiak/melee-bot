import pytest

import config


def test_guild_ids_list():
    assert config.guild_ids({"GUILD_IDS": "123,456"}) == [123, 456]


def test_guild_ids_tolerates_spaces_and_trailing_comma():
    assert config.guild_ids({"GUILD_IDS": " 123 , 456, "}) == [123, 456]


def test_guild_ids_drops_duplicates_keeping_order():
    assert config.guild_ids({"GUILD_IDS": "456,123,456"}) == [456, 123]


def test_guild_id_is_accepted_as_fallback():
    assert config.guild_ids({"GUILD_ID": "123"}) == [123]


def test_guild_ids_wins_over_guild_id():
    assert config.guild_ids({"GUILD_IDS": "456,789", "GUILD_ID": "123"}) == [456, 789]


def test_guild_ids_empty_when_nothing_set():
    assert config.guild_ids({}) == []
    assert config.guild_ids({"GUILD_IDS": "", "GUILD_ID": ""}) == []


@pytest.mark.parametrize(
    "env", [{"GUILD_IDS": "123,abc"}, {"GUILD_ID": "your_server_id"}, {"GUILD_IDS": "0"}]
)
def test_guild_ids_rejects_non_ids(env):
    with pytest.raises(ValueError, match="numeric Discord server IDs"):
        config.guild_ids(env)


def test_legacy_guild_prefers_guild_id():
    assert config.legacy_guild_id({"GUILD_IDS": "456,789", "GUILD_ID": "123"}) == 123


def test_legacy_guild_falls_back_to_first_of_guild_ids():
    assert config.legacy_guild_id({"GUILD_IDS": "456,789"}) == 456


def test_legacy_guild_none_when_nothing_set():
    assert config.legacy_guild_id({}) is None
