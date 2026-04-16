# Contributing

Thanks for taking the time to contribute. This project is small enough that
the process is informal, but a few conventions keep things tidy.

## Development setup

```bash
git clone --recurse-submodules https://github.com/toalba/wows-renderer.git
cd wows-renderer
uv venv
source .venv/bin/activate
uv sync --all-extras
```

The `wows-replay-parser` dependency is pulled from its public Git URL via
`uv sync`. For parallel parser development, check the parser out side by
side and run `uv pip install -e ../wows-replay-parser` after the initial
sync.

Gamedata: see the [README's Setup section](../README.md#setup) — the
submodule is currently private. Public contributors should extract from
their own World of Warships install via `wowsunpack` until the sanitized
public repo lands.

## Running checks locally

```bash
uv run ruff check renderer/ bot/ render_quick.py render_dual.py
uv run mypy renderer/ bot/        # advisory — cairo/discord types are noisy
uv run pytest tests/              # smoke + golden images (skip without fixtures)
```

CI mirrors these on every push and pull request.

## Golden images

Visual regressions are caught by comparing rendered frames against
checked-in reference PNGs in `tests/golden_images/`. If your change
intentionally alters visual output, regenerate the references:

```bash
UPDATE_GOLDEN=1 uv run pytest tests/test_golden_images.py
```

Commit the updated PNGs with the change that caused them. If a golden test
flakes on your machine, investigate the root cause before bumping the MSE
threshold.

## Style

- Line length: **120** characters.
- `from __future__ import annotations` at the top of every module.
- Type hints on every layer's `initialize` + `render` and any public helper.
- Single-render and dual-render layers must be typed against the correct
  context class — see `renderer/layers/base.py::BaseRenderContext /
  SingleRenderContext / DualRenderContext`.

## Commits

Short imperative subject. Body for the *why*. Example:

```
Guard self-color branch behind SingleRenderContext

In dual-render mode replay_a's recording player still has relation==0
in the merged roster; without the isinstance guard one side would
render white instead of team-coloured.
```

## Pull requests

- Branch from `master`.
- One logical change per PR.
- Tests + lint passing.
- Update `CHANGELOG.md` if the change is user-facing.
- Include a rendered frame as a screenshot if the change is visual.

See [`CLAUDE.md`](../CLAUDE.md) for architectural notes on the context
split, layer system, and dual-perspective pipeline.
