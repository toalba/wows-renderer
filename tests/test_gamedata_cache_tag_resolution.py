"""Unit tests for gamedata source-tag resolution.

Two distinct failure modes exist around tag selection:

* **Tag drift** — the pipeline re-creates a tag with different content.
  Covered by ``test_gamedata_cache_tag_drift.py``.
* **Tag substitution** — the requested build has no tag at all, so the
  closest one is used instead. That is *correct* for hotfix builds whose
  data is identical to a neighbouring tag (v12116206 ← v12116141), but
  *wrong* when the repo is simply behind upstream: build 13015811 (patch
  15.7) silently resolved to v12830008 (15.6), which mis-maps the shifted
  Avatar method table and renders every enemy as unspotted.

The distinction is directional: interpolating between two known tags is
safe, while a build newer than every local tag means data is missing.

Uses a real throwaway git repo per test (git is a hard runtime dependency
of the cache system anyway).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from renderer.gamedata_cache import (
    _cache_is_current,
    _newest_tag_build,
    resolve_source_tag,
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _repo_with_tags(tmp_path: Path, *builds: int) -> Path:
    """Git repo with one commit per build, each tagged ``v{build}``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@test")
    _git(repo, "config", "user.name", "test")
    for build in builds:
        (repo / "data.txt").write_text(f"sync {build}\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", f"sync {build}")
        _git(repo, "tag", f"v{build}")
    return repo


def _cache_dir(tmp_path: Path, build: int, sentinel: str) -> Path:
    version_dir = tmp_path / "cache" / f"v{build}"
    version_dir.mkdir(parents=True)
    (version_dir / ".ready").write_text(sentinel)
    return version_dir


# ── newest-tag helper ──────────────────────────────────────────────


def test_newest_tag_build(tmp_path: Path):
    repo = _repo_with_tags(tmp_path, 100, 300, 200)
    assert _newest_tag_build(repo) == 300


def test_newest_tag_build_no_tags(tmp_path: Path):
    repo = _repo_with_tags(tmp_path)
    assert _newest_tag_build(repo) is None


# ── resolution ─────────────────────────────────────────────────────


def test_exact_tag_preferred(tmp_path: Path):
    repo = _repo_with_tags(tmp_path, 100, 200, 300)
    assert resolve_source_tag(repo, "200", allow_fetch=False) == "v200"


def test_interpolated_build_substitutes_closest(tmp_path: Path):
    """A hotfix build between two known tags keeps the old behaviour."""
    repo = _repo_with_tags(tmp_path, 100, 300)
    # 150 is closer to 100 than to 300 → v100, and must NOT raise.
    assert resolve_source_tag(repo, "150", allow_fetch=False) == "v100"


def test_older_than_all_tags_substitutes(tmp_path: Path):
    """Pre-history builds may still resolve; we are not missing new data."""
    repo = _repo_with_tags(tmp_path, 200, 300)
    assert resolve_source_tag(repo, "100", allow_fetch=False) == "v200"


def test_build_newer_than_all_tags_raises(tmp_path: Path):
    """The bug: repo behind upstream must fail loudly, not downgrade."""
    repo = _repo_with_tags(tmp_path, 100, 200)
    with pytest.raises(RuntimeError) as exc:
        resolve_source_tag(repo, "300", allow_fetch=False)
    msg = str(exc.value)
    assert "300" in msg
    assert "fetch" in msg.lower()


def test_no_tags_at_all_raises(tmp_path: Path):
    repo = _repo_with_tags(tmp_path)
    with pytest.raises(RuntimeError):
        resolve_source_tag(repo, "100", allow_fetch=False)


def test_fetch_attempt_on_remoteless_repo_does_not_crash(tmp_path: Path):
    """allow_fetch is best-effort: a repo with no remote still resolves."""
    repo = _repo_with_tags(tmp_path, 100, 200)
    assert resolve_source_tag(repo, "200", allow_fetch=True) == "v200"


# ── substituted-cache invalidation ─────────────────────────────────


def test_substituted_cache_rebuilt_once_exact_tag_appears(tmp_path: Path):
    """A cache built from a downgraded tag must not survive the fetch.

    This is what leaves a wrong render cached indefinitely: the sentinel
    records the tag it was built from, so the drift check compares that
    tag against itself and reports "current".
    """
    repo = _repo_with_tags(tmp_path, 100, 200)
    commit_100 = _git(repo, "rev-parse", "v100^{commit}")
    version_dir = _cache_dir(
        tmp_path, 200, f"v200\ntag=v100\ncommit={commit_100}\n",
    )
    assert _cache_is_current(version_dir, repo, "200") is False


def test_substituted_cache_kept_while_exact_tag_absent(tmp_path: Path):
    """Legitimate substitution must not churn on every render."""
    repo = _repo_with_tags(tmp_path, 100)
    commit_100 = _git(repo, "rev-parse", "v100^{commit}")
    version_dir = _cache_dir(
        tmp_path, 150, f"v150\ntag=v100\ncommit={commit_100}\n",
    )
    assert _cache_is_current(version_dir, repo, "150") is True


def test_exact_tag_cache_still_validated_by_commit(tmp_path: Path):
    """Existing drift detection keeps working for exact-tag caches."""
    repo = _repo_with_tags(tmp_path, 100)
    version_dir = _cache_dir(
        tmp_path, 100, "v100\ntag=v100\ncommit=deadbeef\n",
    )
    assert _cache_is_current(version_dir, repo, "100") is False
