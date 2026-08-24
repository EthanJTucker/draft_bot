# draft_bot

Live auction assistant for a 12-team Sleeper keeper league. Built so far: a
read-only Sleeper API client with an on-disk cache, clean parsed types for
picks and draft state, a snapshot CLI, the static value sheet (room prices,
projection worth, keeper NPV), and live draft tracking with a replay demo.
The backtest report and the dashboard are the later slices. The decision
record lives in GAMEPLAN.md, PRD.md, and RESEARCH.md.

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

## Value sheet CLI

```
python -m draftbot.valuesheet
```

Builds the static pre-draft sheet: the full ranked player pool with, per
player, projection worth (value over replacement in auction dollars), the
room price (empirical median of the league's own 2023-25 winning bids in a
position/ADP band; log-curve fallback only when the band has under six
samples, capped at the position's max observed bid), and the NPV-adjusted
value (worth plus the multi-year keeper option at gamma 0.8, priced from
the league's own year-over-year price transitions with experience
recomputed as-of-season and an age-matched pool for 8+ year RBs). The
model tunables (band ratio, band sample gate, gamma, curve cap) live in
the `[valuation]` section of `league_config.toml`. Writes the CSV where
`--out` points (default `data/value_sheet_<season>.csv` next to the
config, gitignored) and prints the top of the board (`--top`, default 30).
The first run needs the network (or a `data/cache/` already primed by an
earlier value-sheet run); after that every endpoint can be served
from the cache. Two runs over the same cached inputs emit byte-identical
CSV. Keeper rows hiding in the historical picks feeds - flagged
`is_keeper`, or unflagged but matching the same-roster keeper cost-chain
signature - are excluded from the price fit. Exit codes: 0 sheet written
(cache-served endpoints get a warning label), 2 nothing produced (missing,
unparseable, or incomplete config, or an endpoint with neither live data
nor cache).

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

## Repricing engine

`draftbot/draft_engine.py` turns the value sheet plus the live board into
a max bid for a nominated player. `analyze_player(player_id, rows, board,
config)` is a pure function - no stored state, no clock, no network - so
the replay backtest can ask for the engine's number on the board as it
stood before any historical sale folded in (the seam contract: price the
nominee pre-sale, never on a board that already contains his own sale)
and the dashboard only renders the returned record. The record carries
every number the top bar shows: sheet worth, keeper premium, and value;
the position's inflation ratio and the inflation-adjusted price;
marginal lineup worth and the need bump; the spend margin and boost;
tier status; my team's max-bid cap; and the final whole-dollar max bid.
Two guardrails: a board that shows kept players refuses to price without
`keepers_by_slot` (silently mispricing a keeper board is worse than
stopping), and an off-sheet nominee stays at the $1 floor - the pace
boost never burns surplus into players the model does not price.

The layers, in order:

- Positional inflation: each position is budgeted its share of the
  room's initial discretionary money (actual keeper-reduced budgets,
  never the sheet's normalization room) by its share of the pool;
  on-model sales debit it dollar for dollar above the floor, and the
  ratio divides by the remaining pool. Off-model sales debit no
  position's money; a sale of a player the sheet does not price touches
  nothing else either, while a board-flagged sale of a player the sheet
  does price (a defensive path, unreachable when tracker and engine
  share one sheet) still removes that value from the remaining pool -
  sold is sold - so the ratio can move. Kept players never enter a
  pool, and the multiplier tapers to zero past roughly the 130th player
  so the $1-5 tail holds its price.
- Marginal roster need: best legal starting lineup with the player minus
  without, FLEX included. A discount schedule, never a premium: a
  scarce starter keeps the full inflation-adjusted price, a redundant
  player is discounted toward bench retention, and the bump is never
  positive - scarcity pressure comes from inflation, the spend
  schedule, and the last-of-tier flag, so a roster-side bump on top
  would double-count. A third QB adds nothing and prices at bench
  retention; an upgrade adds only its edge over the incumbent; the
  keeper premium is never need-discounted.
- Spend-down schedule: max bids start under value at money parity and
  rise as my money-per-open-slot outpaces the room's, plus a pace-boost
  that burns money the remaining pool can no longer absorb. A simulated
  180-lot auction in the tests must finish with a full roster and no
  meaningful unspent cash.
- Gap-based tiers per position, with remaining-in-tier counts and a
  last-of-tier flag that fires exactly on the final remaining member.

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

## Backtest

```
python -m draftbot.backtest
```

Replays the real 2025 auction (committed fixtures, fully offline) through
the tracker and the repricing engine, pricing every nominee on the board
as it stood before his own sale folded in, and rewrites
`reports/backtest_2025.md`: overall and per-position MAE and bias for the
engine's running (inflation-adjusted) estimate and for the static sheet
price, an early/mid/late drift comparison, the off-model (K/DEF)
exclusions, and the measured-first bounds the pytest gate asserts. The
sheet is honest - room prices fit on the 2023-2024 bids only, applied to
2025 preseason ADP - and the suite regenerates the report byte-for-byte,
so the committed copy can never drift from the code. The replay is also
the dashboard's offline data source: build the sheet with
`draftbot.backtest.build_history_price_sheet`, then drive `ReplaySource`
through `DraftTracker` and price each nominee with `analyze_player` on
the pre-sale board, exactly as `draftbot.backtest.replay_records` does
(its docstring is the reference). Options: `--config`, `--history`,
`--draft`, `--season`, `--out`.
