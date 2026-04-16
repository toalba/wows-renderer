"""Golden-image comparison utilities for renderer regression tests.

Byte-identical hash comparisons are too strict — cairo / cairosvg text and
anti-aliased primitives produce tiny per-pixel nondeterminism across runs
and platforms. Instead we compute a normalized mean-squared-error (MSE)
over RGBA bytes and pass under a small threshold.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

try:
    from PIL import Image
except ImportError:  # pragma: no cover - Pillow is a dev-only dep
    Image = None  # type: ignore[assignment]


GOLDEN_DIR = Path(__file__).parent / "golden_images"
DIFF_DIR = GOLDEN_DIR / "_diffs"


def _reference_path(name: str) -> Path:
    return GOLDEN_DIR / f"{name}.png"


def load_reference(name: str) -> bytes | None:
    """Return PNG bytes for the reference, or None if the file does not exist."""
    path = _reference_path(name)
    if not path.exists():
        return None
    return path.read_bytes()


def _load_rgba(path: Path) -> tuple[tuple[int, int], bytes]:
    if Image is None:
        raise RuntimeError("Pillow is required for golden-image comparison")
    with Image.open(path) as im:
        im = im.convert("RGBA")
        return im.size, im.tobytes()


def _mse(a: bytes, b: bytes) -> float:
    """Normalized MSE in [0, 1]. Assumes len(a) == len(b)."""
    if len(a) == 0:
        return 0.0
    # Python-level loop is fine for modest image sizes (<2MP); avoids numpy dep.
    total = 0
    for x, y in zip(a, b, strict=True):
        d = x - y
        total += d * d
    return total / (len(a) * 255.0 * 255.0)


def _write_diff(
    actual_path: Path,
    reference_path: Path,
    name: str,
) -> Path:
    """Write a side-by-side diff PNG: [actual | reference | per-pixel diff]."""
    assert Image is not None
    DIFF_DIR.mkdir(parents=True, exist_ok=True)
    with Image.open(actual_path) as a_img, Image.open(reference_path) as r_img:
        a = a_img.convert("RGBA")
        r = r_img.convert("RGBA")
        w = max(a.width, r.width)
        h = max(a.height, r.height)
        canvas = Image.new("RGBA", (w * 3, h), (0, 0, 0, 255))
        canvas.paste(a, (0, 0))
        canvas.paste(r, (w, 0))
        # Per-pixel diff (where they match) amplified 8x for visibility
        if a.size == r.size:
            from PIL import ImageChops
            diff = ImageChops.difference(a.convert("RGB"), r.convert("RGB"))
            # Amplify
            diff = diff.point(lambda v: min(255, v * 8))
            canvas.paste(diff.convert("RGBA"), (2 * w, 0))
        out = DIFF_DIR / f"{name}.diff.png"
        canvas.save(out)
        return out


def compare_images(
    actual_png_path: Path,
    reference_name: str,
    *,
    threshold: float = 0.005,
) -> tuple[bool, float]:
    """Compare ``actual`` PNG against the named reference.

    Returns ``(passed, mse)`` where ``passed = mse < threshold``.

    Raises ``FileNotFoundError`` if the reference does not exist — callers
    that want to skip in that case should check :func:`load_reference` first.
    On failure, a side-by-side diff PNG is written under ``_diffs/``.
    """
    if Image is None:
        raise RuntimeError(
            "Pillow is required for golden-image comparison. "
            "Install with: uv pip install 'Pillow>=10'"
        )
    ref_path = _reference_path(reference_name)
    if not ref_path.exists():
        raise FileNotFoundError(f"Reference image not found: {ref_path}")

    a_size, a_bytes = _load_rgba(Path(actual_png_path))
    r_size, r_bytes = _load_rgba(ref_path)

    if a_size != r_size:
        _write_diff(Path(actual_png_path), ref_path, reference_name)
        return False, 1.0

    mse = _mse(a_bytes, r_bytes)
    passed = mse < threshold
    if not passed:
        _write_diff(Path(actual_png_path), ref_path, reference_name)
    return passed, mse


def update_reference(actual_png_path: Path, reference_name: str) -> Path:
    """Copy ``actual`` into the reference slot (gated by ``UPDATE_GOLDEN=1``)."""
    if os.environ.get("UPDATE_GOLDEN") != "1":
        raise RuntimeError(
            "update_reference() requires UPDATE_GOLDEN=1 to avoid "
            "silently overwriting baselines in CI."
        )
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    dest = _reference_path(reference_name)
    shutil.copyfile(actual_png_path, dest)
    return dest
