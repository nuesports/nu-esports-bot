# Add pytest testing infrastructure (closes #49)

## Summary

No test framework existed in this repo before this PR — no `tests/` dir, no
CI check beyond Ruff. Several past bugs (matchmaking role-assignment logic,
elo tuning, an interaction-token expiry bug) were all caught by manual
reading or live reproduction rather than anything automated. This PR adds
`pytest`, a `tests/` suite covering the pure-logic parts of the bot, and a
CI workflow that runs the suite on every push/PR to `main`.

## What's included

- **`pyproject.toml`** — adds `pytest` to dev dependencies, plus
  `[tool.pytest.ini_options] pythonpath = ["."]` so test modules can import
  top-level packages (`utils`, `cogs`) the same way `bot.py` does.
- **`tests/conftest.py`** — stages `config.yaml`/`secrets.yaml` from the
  `.example` files if they're missing, so importing anything that touches
  `utils/config.py` doesn't crash on a machine (or CI runner) that never
  set up real config.
- **`.github/workflows/pytest.yml`** — installs `uv`, runs `uv sync --frozen`,
  then `uv run pytest`. Same shape as the existing `ruff.yml`.
- **Test files**, one per module, covering the parts that don't need a live
  Discord gateway or database connection:
  - `test_elo.py` — rank decoding, rank-to-points curve, elo delta math
  - `test_matchmaking.py` — `balance_teams` role/team assignment
  - `test_profile.py` — rank value/label computation, tier+division
    validation, game-head permission check
  - `test_leaderboard.py` — entry formatting and page-building/pinning logic
  - `test_config.py` — per-role-ranks lookups, missing-file error paths
  - `test_valorant.py` — random map/team generation
  - `test_github_backlog.py` — markdown stripping, GitHub webhook HMAC
    signature verification

## What's intentionally NOT covered

Anything requiring a live gateway connection, real Discord objects, or a
real database connection (most cog command handlers, `utils/db.py`,
`utils/migrate.py`). These would need heavier mocking for comparatively
low payoff — pure logic was the highest-value, lowest-effort target, and
is exactly where the bugs listed above actually lived.

## Test plan

- [ ] `uv run pytest -v` passes locally
- [ ] CI run on this PR is green
- [ ] Existing bot functionality unaffected (no runtime code changed, only
      `pyproject.toml` and new files under `tests/`)

## Follow-up (not in this PR)

- `CONTRIBUTING.md` still says "There is no formal testing framework as of
  writing this guide" — worth updating once this merges.
- Could extend coverage to `cogs/connections.py`'s word-normalization logic
  or `utils/images.py`'s `slugify` if more low-effort wins are wanted later.
