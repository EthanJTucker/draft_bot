# 2025 replay backtest

The real 2025 auction — all 180 sales, $1632 of winning
bids — replayed sale by sale through the tracker and the repricing
engine. At every sale moment the engine prices the nominee on the board
as it stood BEFORE that sale folded in (the pre-sale seam contract), and
the resulting estimates are scored against the amount the room actually
paid.

Method, in five rules:

- **Honest sheet.** Room prices are fit on the 2023-2024 winning bids
  only (the unflagged-keeper detection path runs on the fit and finds
  none in 2023-2024; the exclusion wiring itself is pinned by test on
  a 2025-inclusive fit) and applied to 2025 preseason ADP — the
  post-preseason snapshot, approximately draft-morning: nothing the
  bot could not have known on 2025 draft night. The sheet prices
  377 players (291 band, 14 curve, 3 curve_capped, 69 curve_floor).
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
| running (inflation-adjusted) | 159 | 2.24 | -0.55 |

## Per-position

| position | n | running MAE | running bias | static MAE | static bias |
| --- | --- | --- | --- | --- | --- |
| QB | 21 | 1.83 | +0.31 | 1.83 | +0.31 |
| RB | 56 | 2.22 | -0.17 | 2.22 | -0.17 |
| TE | 18 | 1.78 | +1.28 | 1.78 | +1.28 |
| WR | 64 | 2.51 | -1.69 | 2.51 | -1.69 |

## Early/mid/late drift

| segment | lots | n | running MAE | running bias | static MAE | static bias |
| --- | --- | --- | --- | --- | --- | --- |
| early | 1-60 | 60 | 4.09 | -1.71 | 4.09 | -1.71 |
| mid | 61-120 | 50 | 1.91 | +0.23 | 1.91 | +0.23 |
| late | 121-180 | 49 | 0.31 | +0.06 | 0.31 | +0.06 |

Running bias spread: 1.94. Static bias spread: 1.94.

## Finding: the floor holds every 2025 lot at par

The calibration lives in the static sheet: MAE 2.24, bias
-0.55 — this room's own 2023-2024 prices predict its 2025
prices to within about two dollars a lot, with no meaningful drift
(segment table above). The running estimate multiplies that sheet by
remaining-money-over-remaining-value per position, and on this replay
that ratio sits below par at every scored sale moment: all
159 of the 159 scored lots price at the
1.00 INFLATION_MIN clamp. The running column above is
therefore the static column, lot for lot — MAE
2.24, bias -0.55, segment bias
-1.71 / +0.23 / +0.06
— equal to the static numbers to the last decimal. There is no
early-board drift left because there is no position-level adjustment
left to drift.

Why the raw ratio is below par everywhere: the denominator counts EVERY
unsold sheet row as competing for the room's money, but an auction only
absorbs 180 lots. The overhang never clears — when the last lot closes,
$1116 of above-floor sheet value is still
unsold against the $1464 of discretionary money the
room started with, so about
76% of a room's money worth of
priced value never sells. That denominator alone accounts for below
par: at the opening lot no money has been spent, so every position's
ratio is exactly the room's money over the whole above-floor pool.

Below par is not the floor of it, though, and a denominator re-size
will not reach the rest. On 36 of the
159 scored lots the per-position budget SPLIT has overspent that
position's share, which drives the NUMERATOR negative — measured
minimum -0.2141, every one of those lots WR. Since
the ratio is (budgeted - spent) / left, shrinking the denominator makes
a negative numerator more negative, not less. The floor is a bound on
both defects, not a repair of either: raising it was measured, and the
absorbable-pool change is separate work that addresses the denominator.

What the floor costs, stated plainly. The engine can no longer say
"this position is getting cheaper, bid less" — the deflation half of
the model is discarded, not damped, so a mild overpay and a
catastrophic one now read identically. On this fixture the
inflation-adjusted price is also board-INDEPENDENT: the running column
carries nothing the sheet did not already have. Both costs are accepted
deliberately, on the argument that a below-par reading on THIS sheet is
its overhang and its budget split talking rather than the market
(a production sheet is normalized to the room's money and does not
open below par at all), and both are pinned by
`tests/test_backtest_replay.py` so the absorbable-pool fix has to come
back and re-measure this floor.

For the record, the pre-change measurement that motivated the constant
(engine at INFLATION_MIN 0.25, same fixtures, same sheet): the ratio
opened at 0.599, RB and WR reached the 0.25 clamp before the opening
third was out, 84 of the 159 scored lots priced at exactly that clamp,
and the running estimate scored MAE 5.54 with a -11.81 early-board bias
— an expensive mid-draft nominee displayed at roughly a quarter of his
real price. That measurement is historical and is not regenerated here;
what this report regenerates is the behaviour above.

Draft-night reading: the running estimate can now only sit at or above
the sheet price, so it is safe to read on an expensive nominee at any
point in the draft. What it will not do this season is warn you that a
position has gone cold. For that, read the tier and last-of-tier flags
and the room's remaining money.

## Worst running misses

| lot | player | pos | static | running | actual |
| --- | --- | --- | --- | --- | --- |
| 54 | Ricky Pearsall | WR | 8.50 | 8.50 | 28 |
| 21 | Marvin Harrison | WR | 24.00 | 24.00 | 43 |
| 49 | Patrick Mahomes | QB | 21.50 | 21.50 | 8 |
| 9 | Drake London | WR | 37.00 | 37.00 | 50 |
| 30 | James Conner | RB | 27.00 | 27.00 | 38 |
| 16 | Alvin Kamara | RB | 33.00 | 33.00 | 24 |
| 57 | Jacory Croskey-Merritt | RB | 9.00 | 9.00 | 18 |
| 68 | Mark Andrews | TE | 14.00 | 14.00 | 5 |

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
regression. The running drift bounds are no-systematic-drift
assertions now, not the regression pin of a known deflation
drift they used to be: they point at zero from both sides. They
coincide with the static bounds because the inflation floor
binds on every scored lot here, so the two estimates are the
same numbers — they are separate metrics, not aliases, and the
absorbable-pool change will separate them again.

| metric | measured | bound | margin |
| --- | --- | --- | --- |
| running MAE | 2.24 | 2.75 | 0.60 |
| running bias spread (early/mid/late) | 1.94 | 2.50 | 0.75 |
| running worst segment absolute bias | 1.71 | 2.25 | 0.60 |
| static MAE | 2.24 | 2.75 | 0.60 |
| static bias spread (early/mid/late) | 1.94 | 2.50 | 0.75 |
| static absolute bias | 0.55 | 1.50 | 1.00 |

## Secondary: the max-bid ceiling (bid policy, not a prediction)

The final max bid folds in MY roster need and the spend-down
schedule; it is what the engine would let me pay, not what it
thinks the room will pay. Shown for completeness only:

| segment | lots | n | max bid MAE | max bid bias |
| --- | --- | --- | --- | --- |
| early | 1-60 | 60 | 6.47 | -5.77 |
| mid | 61-120 | 50 | 3.22 | -3.18 |
| late | 121-180 | 49 | 0.22 | -0.22 |
| overall | 1-180 | 159 | 3.52 | -3.25 |

## Reproducing

```
python -m draftbot.backtest
```

Reads `tests/fixtures/league_history.json` and
`tests/fixtures/draft_2025.json`, rewrites this report in place
(`reports/backtest_2025.md`). Fully offline and deterministic;
`tests/test_backtest_replay.py` regenerates it and fails if the
committed copy differs by a byte.
