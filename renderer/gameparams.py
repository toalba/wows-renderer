"""GameParams.data decoding, pickle caching, and data extraction.

GameParams.data is a binary file shipped with World of Warships containing all
game entity data (ships, modules, consumables, projectiles, aircraft, etc.).

Encoding: bytes reversed → zlib compressed → Python 2 pickle with custom
GameParams.* module classes (dict subclasses).

At runtime we decode using a custom unpickler that maps all GameParams.* classes
to GPObject (a plain dict subclass with __setstate__), yielding a ~15MB nested
Python dict.

The decoded result is cached as a standard Python pickle, keyed by a truncated
blake2b hash of the source binary. Warm load is a single pickle.load() call.
"""

from __future__ import annotations

import copyreg
import hashlib
import io
import json
import logging
import pickle
import zlib
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# ── GameParams decode ──────────────────────────────────────────────


class GPObject(dict):
    """Stand-in for GameParams module classes (dict subclasses in the pickle)."""

    def __setstate__(self, state: dict) -> None:
        if isinstance(state, dict):
            self.update(state)


class GameParamsUnpickler(pickle.Unpickler):
    """Custom unpickler that maps GameParams.* classes to GPObject."""

    def find_class(self, module: str, name: str) -> type:
        if module == "GameParams":
            return GPObject
        return super().find_class(module, name)


def decode_gameparams(path: Path) -> dict:
    """Decode GameParams.data → Python dict.

    Format: reverse all bytes → zlib decompress → pickle load.
    """
    raw = path.read_bytes()
    reversed_data = raw[::-1]
    decompressed = zlib.decompress(reversed_data)

    # Patch copyreg._reconstructor to handle dict subclass creation.
    # Not thread-safe, but fine with ProcessPoolExecutor (separate processes).
    original = copyreg._reconstructor

    def patched(cls, base, state):
        if issubclass(cls, dict):
            obj = dict.__new__(cls)
            if state is not None:
                dict.__init__(obj, state)
            return obj
        return original(cls, base, state)

    copyreg._reconstructor = patched
    try:
        gp = GameParamsUnpickler(
            io.BytesIO(decompressed), encoding="latin1"
        ).load()
    finally:
        copyreg._reconstructor = original

    # Navigate to inner dict (gp is a list with one element containing a '' key)
    if isinstance(gp, (list, tuple)):
        gp = gp[0]
    if isinstance(gp, dict) and "" in gp:
        gp = gp[""]
    return gp


# ── Pickle cache ───────────────────────────────────────────────────

_HASH_DIGEST_SIZE = 16  # 128-bit blake2b truncation


def _compute_hash(path: Path) -> str:
    """Compute truncated blake2b hex digest of a file."""
    h = hashlib.blake2b(digest_size=_HASH_DIGEST_SIZE)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def decode_and_cache_gameparams(source_path: Path, cache_dir: Path) -> dict:
    """Decode GameParams.data and cache the result as a pickle.

    The cache is keyed by a blake2b hash of the source binary. If the hash
    matches an existing cache, the pickle is loaded directly. Otherwise the
    source is decoded, the result is pickled, and stale caches are evicted.

    Args:
        source_path: Path to GameParams.data binary.
        cache_dir: Directory to store gameparams.pickle + gameparams.blake2b.

    Returns:
        The fully decoded GameParams dict.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    pickle_path = cache_dir / "gameparams.pickle"
    hash_path = cache_dir / "gameparams.blake2b"

    source_hash = _compute_hash(source_path)

    # Check for existing valid cache
    if pickle_path.exists() and hash_path.exists():
        cached_hash = hash_path.read_text().strip()
        if cached_hash == source_hash:
            return load_gameparams_cached(cache_dir)

    # Decode from source
    log.info("Decoding GameParams.data (%s)...", source_path)
    gp = decode_gameparams(source_path)
    log.info("Decoded %d GameParams entities", len(gp))

    # Write cache
    with open(pickle_path, "wb") as f:
        pickle.dump(gp, f, protocol=pickle.HIGHEST_PROTOCOL)
    hash_path.write_text(source_hash)

    return gp


def load_gameparams_cached(cache_dir: Path) -> dict:
    """Load a previously cached GameParams pickle.

    Args:
        cache_dir: Directory containing gameparams.pickle.

    Returns:
        The fully decoded GameParams dict.
    """
    pickle_path = cache_dir / "gameparams.pickle"
    with open(pickle_path, "rb") as f:
        return pickle.load(f)


# ── Split file generation ──────────────────────────────────────────


def make_serializable(obj: Any) -> Any:
    """Recursively convert non-JSON-serializable types."""
    if isinstance(obj, dict):
        return {str(k): make_serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [make_serializable(v) for v in obj]
    if isinstance(obj, bytes):
        try:
            return obj.decode("latin1")
        except Exception:
            return obj.hex()
    if isinstance(obj, (int, float, str, bool, type(None))):
        return obj
    return str(obj)


def write_split_subset(gp: dict, output_dir: Path, types: set[str]) -> int:
    """Write per-entity JSON files for specified entity types only.

    Creates output_dir/split/{TypeName}/{EntityName}.json for each entity
    whose typeinfo.type is in the types set.

    Args:
        gp: Decoded GameParams dict.
        output_dir: Root directory (split/ subdir is created inside).
        types: Set of entity type names to include (e.g. {"Modernization", "Crew"}).

    Returns:
        Number of files written.
    """
    count = 0
    split_dir = output_dir / "split"
    for name, obj in gp.items():
        if not isinstance(obj, dict):
            continue
        ti = obj.get("typeinfo")
        if not isinstance(ti, dict):
            continue
        entity_type = ti.get("type", "")
        if entity_type not in types:
            continue
        type_dir = split_dir / entity_type
        type_dir.mkdir(parents=True, exist_ok=True)
        out = type_dir / f"{name}.json"
        out.write_text(json.dumps(make_serializable(obj), indent=4))
        count += 1
    return count


def split_by_type(gp: dict, output_dir: Path) -> None:
    """Split GameParams into per-entity JSON files by typeinfo.type.

    Writes output_dir/{TypeName}/{EntityName}.json for every entity.
    """
    for name, obj in gp.items():
        if not isinstance(obj, dict):
            continue
        ti = obj.get("typeinfo")
        if not isinstance(ti, dict):
            continue
        entity_type = ti.get("type", "Other")
        type_dir = output_dir / entity_type
        type_dir.mkdir(parents=True, exist_ok=True)
        out = type_dir / f"{name}.json"
        out.write_text(json.dumps(make_serializable(obj), indent=4))
