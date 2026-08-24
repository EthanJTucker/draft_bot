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
season projections into `data/cache/` (gitignored, resolved next to the
config file) and prints a league summary with teams in draft-slot order,
budgets, keeper counts, and draft status. The snapshot degrades per
endpoint: if a live fetch fails, the cached copy is served and labeled in
the summary, and endpoints with no cache are listed as unavailable. Exit
codes: 0 everything snapshotted, 1 summary printed but some endpoint had no
data, 2 nothing ran (bad config path). Options: `--config` for an alternate
league config, `--cache-dir` for an alternate cache location.

Note for pre-August-2026 checkouts: draft and picks caches used to be written
as un-keyed `draft.json`/`picks.json`; those filenames are now orphaned, so
clear `data/cache/` once (or delete just those two files) after updating.

League facts (IDs, budgets, roster shape, keeper constants) and the HTTP
request timeout live in `league_config.toml`, not in code. The client can
also fetch prior seasons' drafts (`get_draft(draft_id=...)` /
`get_picks(draft_id=...)`); each draft caches under its own id, so
historical fetches never touch the live draft's fallback cache.

## Draft tracker and replay demo

`draftbot/sources.py` and `draftbot/tracker.py` are the live draft-state
slice. A live poll source and a historical replay source present the same
one-method interface (`poll() -> SourceTick`), so the tracker cannot tell
which one feeds it; later slices drive the backtest and the dashboard
through the same seam. The tracker folds ticks into a board of per-team
budgets (seeded from `budget_<slot>` once the commissioner enters keepers,
the config default until then, labeled as defaulted), spend, open slots,
positional needs, and max possible bid (remaining budget minus $1 for
every other open slot). Its guards: a nominee already in the sold set
never renders as live (Sleeper's nomination pointer keeps naming the
just-sold winner until the next lot opens), a degraded or regressed picks
feed never rules a lot live either (the sold set could be missing a
recent sale, so the nomination shows as `untrusted` instead), settings
mismatches (type, teams, budget, timers, keeper budgets missing for any
keeper team) surface as warnings, a paused draft never reads as an
expired timer, and sales absent from an optionally injected value sheet
are flagged off-model for the inflation math to exclude while still
debiting the buying team's budget.

```
python -m draftbot.trackdemo
```

Replays the 2025 draft sale by sale (buying-team state per pick, the full
board every `--table-every` sales) and verifies each team's final spend
against the 2025 draft object's own `budget_<slot>`. Exit codes: 0
verified, 1 a team overspent its budget, 2 config or fetch failure.
Options: `--year`, `--config`, `--cache-dir`, `--table-every`.

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
