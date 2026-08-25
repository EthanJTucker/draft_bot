# draft_bot

Live auction assistant for a 12-team Sleeper keeper league. Built so far: a
read-only Sleeper API client with an on-disk cache, clean parsed types for
picks and draft state, a snapshot CLI, the static value sheet (room prices,
projection worth, keeper NPV), and live draft tracking with a replay demo.
The backtest report and the dashboard are built too, and the override
file, the allocation plan, nomination suggestions, and the dry run are
the later slices. The
decision record lives in GAMEPLAN.md, PRD.md, and RESEARCH.md.

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
which one feeds it; the backtest and the dashboard drive it through the
same seam. The tracker folds ticks into a board of per-team
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
so the committed copy can never drift from the code. The backtest and
the dashboard's `--replay` mode drive the same chain - `ReplaySource`
through `DraftTracker`, each nominee priced with `analyze_player` on the
pre-sale board (`draftbot.backtest.replay_records` is the reference) -
but each builds its own sheet: the backtest fits the honest history
sheet with `draftbot.backtest.build_history_price_sheet`, while the
dashboard derives its demo sheet from the replay's own hammer prices
unless `--sheet` supplies a real one. Options: `--config`, `--history`,
`--draft`, `--season`, `--out`.

## Dashboard

```
python -m draftbot.dashboard --replay tests/fixtures/draft_2025.json --accelerate 4
```

Serves one auto-refreshing page at `http://127.0.0.1:8724` from a `/state`
JSON endpoint, polled once per cycle (~1s live; `--accelerate` divides the
interval in replay). The top bar shows the nominated player, current high
bid, worth, room price, my max bid, a plain BID/PASS verdict (BID exactly
when the high bid is below my max; equality can only match, so it is
PASS), profit as price-minus-value centered at $0, and the last-of-tier
warning. Below it: the sortable/filterable positional table of remaining
players, every team's budget as remaining dollars (never spent) with open
slots and max possible bid, and my roster and remaining budget pinned.
Fail-closed rendering: an untrusted picks feed, a cache-served draft
object, a paused draft, a lot with no recorded high bid, or a budget
nobody entered for my slot shows a banner or reason and suppresses the
verdict; any poll failure (source outage or
processing error) keeps serving the last good state labeled as such; and
a nomination pointer naming a just-sold player is priced against the
pre-sale board and labeled final — or refuses with the reason when no
board from just before that sale was observed. If nomination metadata is
missing entirely, values, budgets, and sold players still render from
picks polling.

The replay command above needs nothing but the checked-in fixture: it
derives a demo sheet from the replay's own hammer prices (so profit reads
$0 and the bargain-margin bot rationally PASSes every lot; the layout is
the point — and the page footer says so). Pass `--sheet
data/value_sheet_2026.csv` to price the replay against a real value sheet
instead, with varying profits and verdicts. Live mode (no `--replay`)
requires `--sheet` and pulls keeper lists from the league's rosters at
startup; if this keeper league's rosters come back with zero keepers the
dashboard refuses to start (pricing a keeper board keeper-free would be
silently wrong) unless `--allow-no-keepers` says the league truly kept no
one. Options: `--config`, `--cache-dir`, `--host`, `--port`,
`--interval`, `--accelerate`, `--my-slot` (defaults to the config's
roster id resolved against the draft; an out-of-range slot is rejected at
startup), `--allow-no-keepers`, `--budget`. Exit codes: 0 served and shut
down cleanly, 2 nothing ran.

Per-team budgets are the one number Sleeper may not give you. It carries
them only as `draft.settings.budget_<slot>`, which exist only once the
commissioner enters each team's post-keeper money; until then every team
reads at the league-wide default, which on a keeper board overstates the
whole room and never converges (remaining is budget minus spend, so a
wrong budget stays wrong to the last lot). Any slot in that state is
marked, and my own defaulted budget suppresses the verdict rather than
advising off a number the tool made up. `--budget SLOT=AMOUNT` keys the
real figures by hand from the league sheet, repeatably
(`--budget 4=96 --budget 11=143`). AMOUNT is the team's post-keeper money
for the whole draft, the same quantity `budget_<slot>` carries, not a
running balance.

SLOT is the **draft slot**, which is not the roster id. Sleeper assigns
draft order at draft time and the two identifiers then differ: every
completed season in this league maps them differently. The My-team panel
names my draft slot, the team table's first column is the draft slot, and
startup echoes every override it parsed. If overrides are given while my
own resolved slot still has real money from neither source, startup says
so and names the slot to use.

Precedence: an explicit `--budget` beats a live `budget_<slot>` key,
which beats the `[auction] budget` default. Operator input outranks
remote data because the case this lever exists for is a commissioner who
enters the flat league budget for twelve keeper teams: every key is
present, nothing is flagged, and every number is still fiction. Under the
other ordering there is no lever at all. Replacing a figure Sleeper
actually supplied is never silent, though: a standing banner names the
slot and both amounts.

Money a keeper roster provably cannot have left over gets its own banner,
whoever entered it. Keeper cost has a floor, so a slot holding three
keepers cannot carry more than $185 of post-keeper money, and a $200
there is wrong rather than merely unentered — including in the flat
league-budget room above, where nothing else fires. The banner names the
source, and it leaves alone any slot the missing-budget banner already
names: that is one hole, and reporting it twice is how a banner becomes
wallpaper.

The verdict suppression is per-slot on purpose. Keying my own slot
restores my verdict even while the rest of the room is uncovered, because
a tool that blanks for all 180 lots would be worse than one that shows a
labeled figure. That narrowness has a cost, and the page shows it: my max
bid is computed from all twelve budgets (inflation and the pace pool both
read the whole room), so it renders amber and reads `(room default)` for
as long as any keeper team's money is still fabricated, even after my own
verdict has come back.
