"""Unit tests for gamedata cache tag-drift detection.

The gamedata pipeline occasionally re-creates a version tag with fixed
content (seen with v12830008 on patch 15.6: entity_defs changed after the
first sync). The `.ready` sentinel records the tag commit so the fast
path can detect the drift and rebuild instead of silently misparsing.

Uses a real throwaway git repo per test (git is a hard runtime
dependency of the cache system anyway).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from renderer.gamedata_cache import (
    _cache_is_current,
    _discard_version_dir,
    _read_sentinel,
    _tag_commit,
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def tagged_repo(tmp_path: Path) -> Path:
    """Git repo with one commit tagged v100."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@test")
    _git(repo, "config", "user.name", "test")
    (repo / "data.txt").write_text("first sync\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "sync 100")
    _git(repo, "tag", "v100")
    return repo


def _make_cache_dir(tmp_path: Path, sentinel: str) -> Path:
    version_dir = tmp_path / "cache" / "v100"
    version_dir.mkdir(parents=True)
    (version_dir / ".ready").write_text(sentinel)
    return version_dir


def test_tag_commit_resolves(tagged_repo: Path):
    commit = _tag_commit(tagged_repo, "v100")
    assert commit == _git(tagged_repo, "rev-parse", "HEAD")


def test_tag_commit_missing_tag_returns_none(tagged_repo: Path):
    assert _tag_commit(tagged_repo, "v999") is None


def test_tag_commit_non_repo_returns_none(tmp_path: Path):
    assert _tag_commit(tmp_path, "v100") is None


def test_read_sentinel_parses_tag_and_commit(tmp_path: Path):
    version_dir = _make_cache_dir(tmp_path, "v100\ntag=v100\ncommit=abc123\n")
    info = _read_sentinel(version_dir)
    assert info == {"tag": "v100", "commit": "abc123"}


def test_read_sentinel_missing_returns_none(tmp_path: Path):
    version_dir = tmp_path / "cache" / "v100"
    version_dir.mkdir(parents=True)
    assert _read_sentinel(version_dir) is None


def test_cache_current_when_commit_matches(tagged_repo: Path, tmp_path: Path):
    commit = _git(tagged_repo, "rev-parse", "HEAD")
    version_dir = _make_cache_dir(
        tmp_path, f"v100\ntag=v100\ncommit={commit}\n",
    )
    assert _cache_is_current(version_dir, tagged_repo, "100") is True


def test_cache_stale_when_tag_recreated(tagged_repo: Path, tmp_path: Path):
    old_commit = _git(tagged_repo, "rev-parse", "HEAD")
    version_dir = _make_cache_dir(
        tmp_path, f"v100\ntag=v100\ncommit={old_commit}\n",
    )
    # Pipeline re-creates the tag on a new commit (the v12830008 incident)
    (tagged_repo / "data.txt").write_text("re-sync with fixed defs\n")
    _git(tagged_repo, "add", ".")
    _git(tagged_repo, "commit", "-m", "sync 100 fixed")
    _git(tagged_repo, "tag", "-f", "v100")

    assert _cache_is_current(version_dir, tagged_repo, "100") is False


def test_cache_trusted_when_tag_pruned(tagged_repo: Path, tmp_path: Path):
    """Old versions whose tags no longer exist must keep working."""
    version_dir = _make_cache_dir(
        tmp_path, "v100\ntag=v100\ncommit=abc123\n",
    )
    _git(tagged_repo, "tag", "-d", "v100")
    assert _cache_is_current(version_dir, tagged_repo, "100") is True


def test_cache_trusted_when_git_unavailable(tmp_path: Path):
    """Non-repo gamedata path → cannot validate → trust the cache."""
    version_dir = _make_cache_dir(
        tmp_path, "v100\ntag=v100\ncommit=abc123\n",
    )
    assert _cache_is_current(version_dir, tmp_path, "100") is True


def test_legacy_sentinel_rebuilds_when_tag_exists(
    tagged_repo: Path, tmp_path: Path,
):
    """Pre-drift-detection sentinel + live tag → rebuild once."""
    version_dir = _make_cache_dir(tmp_path, "v100\n")
    assert _cache_is_current(version_dir, tagged_repo, "100") is False


def test_legacy_sentinel_trusted_when_tag_pruned(
    tagged_repo: Path, tmp_path: Path,
):
    version_dir = _make_cache_dir(tmp_path, "v100\n")
    _git(tagged_repo, "tag", "-d", "v100")
    assert _cache_is_current(version_dir, tagged_repo, "100") is True


def test_discard_version_dir_removes(tmp_path: Path):
    version_dir = _make_cache_dir(tmp_path, "v100\n")
    (version_dir / "data").mkdir()
    (version_dir / "data" / "f.txt").write_text("x")
    _discard_version_dir(version_dir, "100")
    assert not version_dir.exists()
    # No stale leftovers in the cache root either
    assert list(version_dir.parent.iterdir()) == []


def test_discard_version_dir_tolerates_missing(tmp_path: Path):
    version_dir = tmp_path / "cache" / "v100"  # never created
    _discard_version_dir(version_dir, "100")  # must not raise
