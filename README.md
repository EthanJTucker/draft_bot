# draft_bot

Live auction assistant for a 12-team Sleeper keeper league. This slice is the
IO boundary: a read-only Sleeper API client with an on-disk cache, clean parsed
types for picks and draft state, and a snapshot CLI. Valuation, live draft
tracking, and the dashboard come in later slices. The decision record lives in
GAMEPLAN.md, PRD.md, and RESEARCH.md.

## Setup

```
conda create -n draft-bot python=3.11
conda activate draft-bot
python -m pip install -r requirements.txt -r requirements-dev.txt
```

## Snapshot CLI

```
python -m draftbot.snapshot
```

Fetches the league, draft, picks, rosters, users, the daily player map, and
season projections into `data/cache/` (gitignored) and prints a league summary
with teams, budgets, keeper counts, and draft status. Options: `--config` for
an alternate league config, `--cache-dir` for an alternate cache location.

League facts (IDs, budgets, roster shape, keeper constants) live in
`league_config.toml`, not in code.

## Tests and lint

```
pytest
black --check .
isort --check-only .
pylint draftbot tests --fail-under=10
```

Unit tests never touch the live network. The client's transport is injectable,
and the suite includes traps for Sleeper's known gotchas: CDN cache-busting on
every poll, bid amounts arriving as strings, purchase attribution by draft
slot, and the pause flag freezing the draft timer.
