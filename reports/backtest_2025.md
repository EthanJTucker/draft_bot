# 2025 replay backtest

The real 2025 auction — all 180 sales, $1632 of winning
bids — replayed sale by sale through the tracker and the repricing
engine. At every sale moment the engine prices the nominee on the board
as it stood BEFORE that sale folded in (the pre-sale seam contract), and
the resulting estimates are scored against the amount the room actually
paid.

Method, in five rules:

- **Honest sheet.** Room prices are fit on the 2023-2024 winning bids
  only (unflagged keeper rows detected and excluded) and applied to 2025
  preseason ADP: nothing the bot could not have known on 2025 draft
  night. The sheet prices 377 players (291 band, 14 curve, 3 curve_capped, 69 curve_floor).
  The keeper premium is deliberately zero: the empirical fit already
  contains the room's keeper appetite, and stacking the model's premium
  on top would double-count it in a market estimate.
- **Pre-sale boards.** A nominee is never priced on a board that already
  contains his own sale (post-sale pricing removes him from his own
  pool, debits his own dollars, and on lots my team won collapses the
  bid to a bench-retention number).
- **Three estimates.** *running* = the engine's inflation-adjusted price
  (the market estimate under test); *static* = the sheet price alone
  (the diagnostic baseline); *max bid* = the engine's bid ceiling —
  bid POLICY, reported only as a labeled secondary comparison.
- **Error convention.** error = estimate - actual; bias is the mean
  signed error (negative = the estimate ran under the room), MAE the
  mean absolute error.
- **Exclusions.** Sales the sheet does not price are off-model: flagged
  by the engine, excluded from every statistic, counted and listed
  below. 21 of 180 lots are off-model here
  ($37 of the room's $1632);
  159 lots are scored.

## Headline: the running estimate (gate metric)

| estimate | n | MAE | bias |
| --- | --- | --- | --- |
| running (inflation-adjusted) | 159 | 5.54 | -5.31 |

## Per-position

| position | n | running MAE | running bias | static MAE | static bias |
| --- | --- | --- | --- | --- | --- |
| QB | 21 | 1.95 | -1.31 | 1.83 | +0.31 |
| RB | 56 | 6.72 | -6.58 | 2.22 | -0.17 |
| TE | 18 | 1.36 | -0.61 | 1.78 | +1.28 |
| WR | 64 | 6.86 | -6.83 | 2.51 | -1.69 |

## Early/mid/late drift

| segment | lots | n | running MAE | running bias | static MAE | static bias |
| --- | --- | --- | --- | --- | --- | --- |
| early | 1-60 | 60 | 11.93 | -11.81 | 4.09 | -1.71 |
| mid | 61-120 | 50 | 3.02 | -2.69 | 1.91 | +0.23 |
| late | 121-180 | 49 | 0.28 | -0.02 | 0.31 | +0.06 |

Running bias spread: 11.80. Static bias spread: 1.94.

## Finding: the running estimate deflates the early board

The calibration lives in the static sheet: MAE 2.24, bias
-0.55 — this room's own 2023-2024 prices predict its 2025
prices to within about two dollars a lot, with no meaningful drift
(segment table above). The running estimate then multiplies that sheet
by remaining-money-over-remaining-value per position, and that ratio
opens far below par (inflation spans 0.25-0.60 across the
replay, never reaching 1.0): the engine's denominator counts EVERY
unsold sheet row as competing for the room's money, but an auction only
absorbs 180 lots — the sheet's priced pool carries roughly a room's
worth of value that nobody will ever buy. The result is a deflation
drift with a fixed shape: the early board runs -11.81
per lot while the taper-protected tail holds sticker, the gap fades as
the overhang clears (-2.69 mid), and the late board
prices on the money (-0.02). Overall the running
estimate scores MAE 5.54, bias -5.31 — strictly worse
than the static sheet it adjusts, on this fixture.

The shape is not an artifact of the sheet's normalization basis:
rescaling the sheet's above-floor prices so the engine's
worth-normalization contract holds exactly, or cutting the pool to the
top 180, reproduces the same early-deep, late-flat drift within a
fraction of a dollar (measured during development against the same
fixtures). Draft-night reading: trust the static column and the late
board; treat the early-board running estimate as a floor, not a fair
price, whenever the room's budgets are far below the sheet's pool
value.

## Worst running misses

| lot | player | pos | static | running | actual |
| --- | --- | --- | --- | --- | --- |
| 21 | Marvin Harrison | WR | 24.00 | 10.36 | 43 |
| 9 | Drake London | WR | 37.00 | 19.20 | 50 |
| 5 | CeeDee Lamb | WR | 54.11 | 31.50 | 61 |
| 30 | James Conner | RB | 27.00 | 11.71 | 38 |
| 54 | Ricky Pearsall | WR | 8.50 | 2.88 | 28 |
| 3 | Derrick Henry | RB | 54.50 | 31.98 | 57 |
| 18 | Josh Jacobs | RB | 48.50 | 25.43 | 49 |
| 1 | Amon-Ra St. Brown | WR | 47.00 | 28.54 | 51 |

## Off-model lots (excluded from every statistic)

| lot | player | pos | actual |
| --- | --- | --- | --- |
| 66 | Denver Broncos | DEF | 3 |
| 92 | Philadelphia Eagles | DEF | 5 |
| 93 | Buffalo Bills | DEF | 1 |
| 95 | Jake Bates | K | 2 |
| 104 | Pittsburgh Steelers | DEF | 1 |
| 105 | Brandon Aubrey | K | 4 |
| 108 | Minnesota Vikings | DEF | 1 |
| 116 | Cameron Dicker | K | 1 |
| 117 | Jake Elliott | K | 3 |
| 120 | Evan McPherson | K | 2 |
| 124 | Detroit Lions | DEF | 1 |
| 131 | Chris Boswell | K | 1 |
| 132 | Chase McLaughlin | K | 1 |
| 134 | Ka'imi Fairbairn | K | 3 |
| 135 | Baltimore Ravens | DEF | 2 |
| 136 | Harrison Butker | K | 1 |
| 143 | Washington Commanders | DEF | 1 |
| 155 | Houston Texans | DEF | 1 |
| 161 | San Francisco 49ers | DEF | 1 |
| 177 | Tyler Loop | K | 1 |
| 179 | Ryan Coe | K | 1 |

## The pytest gate

Bounds were set AFTER measuring (the values below), with the
stated margins as regression headroom. The suite asserts each
metric under its bound AND each bound within its margin of the
measured value, so a loose stale bound fails the same as a
regression. The running bias spread bound is a REGRESSION PIN of
the measured deflation drift documented above — deliberately not
a no-systematic-drift certificate; that claim is asserted where
it is true, on the static sheet.

| metric | measured | bound | margin |
| --- | --- | --- | --- |
| running MAE | 5.54 | 6.50 | 1.00 |
| running bias spread (early/mid/late) | 11.80 | 13.00 | 1.50 |
| static MAE | 2.24 | 2.75 | 0.60 |
| static bias spread (early/mid/late) | 1.94 | 2.50 | 0.75 |
| static absolute bias | 0.55 | 1.50 | 1.00 |

## Secondary: the max-bid ceiling (bid policy, not a prediction)

The final max bid folds in MY roster need and the spend-down
schedule; it is what the engine would let me pay, not what it
thinks the room will pay. Shown for completeness only:

| segment | lots | n | max bid MAE | max bid bias |
| --- | --- | --- | --- | --- |
| early | 1-60 | 60 | 13.70 | -13.63 |
| mid | 61-120 | 50 | 3.76 | -3.76 |
| late | 121-180 | 49 | 0.22 | -0.22 |

## Reproducing

```
python -m draftbot.backtest
```

Reads `tests/fixtures/league_history.json` and
`tests/fixtures/draft_2025.json`, rewrites this report in place
(`reports/backtest_2025.md`). Fully offline and deterministic;
`tests/test_backtest_replay.py` regenerates it and fails if the
committed copy differs by a byte.
