# Renderer test fixtures

Sanitised replay artefacts for the renderer smoke / golden-image tests. Mirrors
the layout and sanitisation policy of the parser repo's
`tests/fixtures/README.md` — please read that first; only the renderer-specific
differences are documented here.

---

## Layout

```
tests/fixtures/
├── README.md                 # (committed)
├── replays/                  # .wowsreplay files (NOT committed)
│   ├── 15_2_0_solo_dd.wowsreplay
│   ├── paired_alpha.wowsreplay
│   └── paired_bravo.wowsreplay
├── gamedata/                 # entity_defs tree + data/ (NOT committed, usually symlinked)
└── outputs/                  # transient render outputs (NOT committed)
```

`outputs/` is for rendered `.mp4` files produced by tests. It is created
on-demand by the conftest; anything written there is disposable. Tests should
prefer `tmp_path` where possible — this directory exists only for debugging
runs where you want the output to survive the test process.

---

## Gamedata

The renderer needs a **full** gamedata tree, not just `entity_defs` — minimap
PNGs, `ships.json`, `map_sizes.json`, `ship_consumables.json`, ribbon and ship
icons, etc. The `fixture_gamedata_path` fixture resolves in this order:

1. `tests/fixtures/gamedata/` (symlink to your `wows-gamedata/data` checkout).
2. `wows-gamedata/data/` at the repo root.
3. Otherwise `pytest.skip(...)`.

Symlink recommended:

```bash
cd tests/fixtures
ln -s ../../wows-gamedata/data gamedata
```

---

## Smoke render test

`tests/test_smoke.py` runs a full render at a throttled configuration
(low minimap size, short duration, coarse fps) to keep the wall-clock
reasonable. It still requires FFmpeg on `$PATH` and a working Cairo install —
if either is missing the test will error out; that's a real environment
problem, not a fixture issue.

Expected runtime with a typical fixture: 10-30 seconds per smoke test.

---

## Sanitisation

Same policy as the parser repo — see
`../../wows-replay-parser/tests/fixtures/README.md`. The renderer does not
re-sanitise; it consumes already-sanitised fixtures. **Never drop a raw replay
into this directory.**

---

## What gets committed

| Path                                      | Committed? |
| ----------------------------------------- | ---------- |
| `tests/fixtures/README.md`                | yes        |
| `tests/fixtures/replays/*.wowsreplay`     | **no**     |
| `tests/fixtures/gamedata/`                | **no**     |
| `tests/fixtures/outputs/`                 | **no**     |

All three non-README paths are in the repo `.gitignore`.
