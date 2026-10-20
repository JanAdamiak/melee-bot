import pytest

import domain.db as db


@pytest.fixture(autouse=True)
def _never_touch_the_real_db(tmp_path, monkeypatch):
    """Every test gets its own database file, whether or not it asks for one,
    so no test can open (or migrate) the developer's real melee.db."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
