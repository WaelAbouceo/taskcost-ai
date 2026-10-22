import pytest

import taskcost.hosting as hosting
import taskcost.reviewer as reviewer
import taskcost.usage as usage


@pytest.fixture(autouse=True)
def no_real_hosting_snapshot(tmp_path, monkeypatch):
    """Tests never read the hosting agent's real snapshot from this machine."""
    monkeypatch.setattr(hosting, "SNAPSHOT_FILE", tmp_path / "hosting.json")
    monkeypatch.setattr(hosting, "HISTORY_DIR", tmp_path / "hosting-history")
    hosting._snapshot_cache.clear()


@pytest.fixture(autouse=True)
def no_real_usage_log(tmp_path, monkeypatch):
    """Tests never write to, or review, this machine's real usage log."""
    monkeypatch.setattr(usage, "USAGE_DIR", tmp_path / "usage")
    monkeypatch.setattr(reviewer, "REVIEWS_DIR", tmp_path / "reviews")
