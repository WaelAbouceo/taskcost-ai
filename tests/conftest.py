import pytest

import taskcost.hosting as hosting


@pytest.fixture(autouse=True)
def no_real_hosting_snapshot(tmp_path, monkeypatch):
    """Tests never read the hosting agent's real snapshot from this machine."""
    monkeypatch.setattr(hosting, "SNAPSHOT_FILE", tmp_path / "hosting.json")
    monkeypatch.setattr(hosting, "HISTORY_DIR", tmp_path / "hosting-history")
    hosting._snapshot_cache.clear()
