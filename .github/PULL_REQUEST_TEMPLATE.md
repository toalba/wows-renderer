<!--
Short imperative subject. Body should answer *why* the change is being
made — the diff already shows *what*.
-->

## Summary



## Type

<!-- Check one -->
- [ ] Bug fix
- [ ] Feature
- [ ] Refactor / cleanup
- [ ] Docs
- [ ] CI / tooling

## Test plan

<!-- How did you verify this works? Delete rows that don't apply. -->
- [ ] `uv run pytest tests/` green
- [ ] `uv run ruff check renderer/ bot/` clean
- [ ] Smoke-rendered a fixture replay locally
- [ ] Regenerated golden images if the change was visual (committed PNGs)

## Visual proof

<!-- If this changes the rendered output, paste a before/after frame here. -->

## Checklist

- [ ] CHANGELOG updated if this is a user-visible change
- [ ] If adding a layer, typed against the correct render context class
- [ ] If touching the dual-render path, smoke-tested with paired replays

## Related issues

<!-- Fixes #123 / Closes #456 -->
