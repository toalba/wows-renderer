# Golden Images

Reference PNGs for renderer regression tests (`tests/test_golden_images.py`).

Each committed PNG here is a known-good frame rendered at a specific timestamp
from a specific replay + gamedata version. Tests render a fresh frame and
compare against the committed baseline using normalized mean-squared-error
(MSE) over RGBA bytes.

## Generating references

Tests skip cleanly when a reference is missing. To create/refresh them locally:

```bash
UPDATE_GOLDEN=1 uv run pytest tests/test_golden_images.py -v
```

This writes `tests/golden_images/{name}.png` for each test that currently has
no baseline. Review the images visually, then `git add` and commit them
alongside any intentional rendering change.

To refresh a single baseline:

```bash
# Delete the stale reference first, then re-run with UPDATE_GOLDEN=1
rm tests/golden_images/single_t30.png
UPDATE_GOLDEN=1 uv run pytest tests/test_golden_images.py::test_single_render_golden -v
```

The `UPDATE_GOLDEN=1` gate exists so CI never silently overwrites baselines.

## Threshold tuning

Default: `threshold=0.005` MSE (on normalized [0, 1] per-pixel squared error).

This is empirically loose enough to absorb cairo / freetype anti-aliasing
nondeterminism across machines, yet tight enough to catch real regressions
(wrong colors, offset ships, missing layers, broken text).

**Do not bump the threshold to silence flakes.** A failing test means either
(a) an intentional visual change — regenerate the baseline, or (b) a real
regression — fix the bug. If a test becomes genuinely flaky across identical
inputs, investigate the nondeterminism source (usually dict ordering, time.now
leaks, or float-hash randomization) before touching the threshold.

## When to regenerate

- Adding a new layer
- Palette / color changes
- Panel layout tweaks
- Font changes
- Upgrading pycairo / cairosvg / freetype

Commit the new PNGs **in the same commit** that changed rendering, so the
baseline and the code stay in lockstep.

## Debugging failures

On mismatch, a side-by-side diff is written to
`tests/golden_images/_diffs/{name}.diff.png`:

```
[actual | reference | per-pixel diff (amplified 8x)]
```

The `_diffs/` directory is gitignored — it is purely local debug output.
