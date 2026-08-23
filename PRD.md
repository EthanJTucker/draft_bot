# PRD: Draft Bot v1 — Live Auction Assistant for 12th Week Campers

## Problem Statement

Ethan drafts in a 12-team Sleeper auction keeper league on Aug 30, 2026 with
10-second nomination and bid timers. In the room, he has no time to compute
what a nominated player is worth, how keeper economics and budget depletion
have shifted prices, what other teams can still pay, or how a bid fits his
remaining budget. Decisions that reward careful math get made on gut feel in
under ten seconds, and multi-year keeper value is left on the table every
season.

## Solution

A local web dashboard on a second monitor. A Python backend polls the Sleeper
API every ~2 seconds and serves one auto-refreshing page. When a player is
nominated, the top bar already shows the current high bid, the player's worth
to Ethan, the price the room is likely to pay, and Ethan's max bid — a single
number encoding dynamic value, positional inflation, his roster's marginal
need, multi-year keeper NPV (gamma 0.8), and a bargain-early/spend-down-late
schedule. Supporting tables track every team's budget, open slots, needs, and
max possible bid, plus a live budget allocation plan, nomination suggestions,
and a manual override file. A replay of past drafts doubles as backtest and
offline fallback.

## User Stories

1. As the drafter, I want the nominated player's name, position, and current high bid on screen within a poll cycle, so that I never parse the Sleeper UI under the timer.
2. As the drafter, I want a single max-bid number for the nominated player, so that I can decide in under ten seconds.
3. As the drafter, I want to see both "worth to me" and "room will pay" for any player, so that I know whether a bid is a bargain or a reach.
4. As the drafter, I want values re-scaled as money and players leave the pool, so that mid-draft prices reflect actual scarcity, not preseason lists.
5. As the drafter, I want inflation tracked per position, so that an RB run raises remaining RB prices without distorting WRs.
6. As the drafter, I want a player's value adjusted for my roster (starter vs bench marginal value), so that I don't pay starter prices for depth.
7. As the drafter, I want young players credited with keeper option value and aging players discounted, so that my bids encode next year's savings, not just this season.
8. As the drafter, I want my max bid to start below value early and drift above it late, so that I exploit the room's early star overpays and never finish with unspent cash.
9. As the drafter, I want every team's remaining budget, open slots, and max possible bid visible, so that I know who can outbid me before I push a price.
10. As the drafter, I want each team's positional needs shown, so that I can predict who will fight me for a nominated player.
11. As the drafter, I want a budget allocation plan that updates as I buy, so that I always know what I can afford to spend on the next tier.
12. As the drafter, I want nomination suggestions when my turn comes, so that I drain rivals' budgets or land cheap targets deliberately.
13. As the drafter, I want a pre-draft override file for my own tiers, avoid list, and dollar tweaks, so that my judgment overrides the model where I disagree.
14. As the drafter, I want my own roster and remaining budget pinned on screen, so that I never bid money I don't have or fill a slot twice.
15. As the drafter, I want keeper costs and locked keepers pulled from league data automatically, so that every team's true starting budget is right without manual entry.
16. As the drafter, I want the dashboard to keep working from cached values if a live endpoint breaks, so that an API hiccup on draft night doesn't blind me.
17. As the drafter, I want to replay last year's draft through the engine, so that I can verify the numbers behave before trusting them live.
18. As the drafter, I want to dry-run the full stack against a Sleeper mock auction, so that draft night is not the first live test.
19. As the drafter, I want players marked as already rostered/kept excluded from available lists, so that I never target a player who isn't actually available.
20. As the drafter, I want the remaining-player table sortable and filterable by position, so that I can scan my next targets between nominations.
21. As a returning league member, I want the valuation calibrated to this room's actual 2023-25 bids, so that "room will pay" reflects my friends, not a generic market.
22. As a keeper-league player, I want draft prices recorded per player, so that next year's keeper costs (price + $2, $5 floor, 2-year cap) compute themselves.

## Implementation Decisions

- Read-only Sleeper integration; the bot never bids. Poll documented league,
  draft, and picks endpoints ~2s; live nomination state read from draft
  metadata (nominated player, highest offer, offering team). Player map
  cached daily; everything cached at startup for graceful degradation.
- Season projections and ADP come from Sleeper's undocumented projections
  endpoint (verified live for 2023-2026); a startup snapshot is the fallback.
- Room price = empirical median of the league's own 2023-25 winning bids in a
  position/ADP band, log-curve fallback below 6 samples. The raw log curve is
  known to overprice top RBs and deep QBs; band medians are authoritative.
- Worth to me = projection-based value over replacement, converted to dollars
  against the remaining pool, adjusted to marginal value given my current
  roster (best-lineup-with minus best-lineup-without).
- Multi-year keeper NPV at gamma 0.8: bid value adds discounted expectations
  of next-year and year-two keep options at cost +$2/+$4, estimated from the
  league's own year-over-year price transitions. Experience must be computed
  as-of-season (the projections API embeds a current-snapshot years_exp);
  RBs with 8+ years use an age-matched comparable pool.
- Max bid applies positional inflation (remaining money / remaining value per
  position) and a spend-down schedule keyed to my money-per-open-slot vs the
  pool; manual override file is applied last and wins.
- Deep modules, each testable in isolation behind a small interface: the
  Sleeper client (IO boundary), the valuation engine (data in, dollars out),
  the draft engine (draft state in, recommendations out), and the replay
  harness (historical draft in, engine calls out). The web layer is a thin
  JSON endpoint plus one static page.
- League config (IDs, budgets, roster shape, keeper rule constants) is data,
  not code, so next season is a config change.
- Environment per Ethan's standard: Python 3.11 conda env, FastAPI + uvicorn,
  pytest, black/isort, pylint at 10.00.

## Testing Decisions

- Tests assert external behavior (inputs to outputs of the module interface),
  never internals. Anti-cheat traps: each test must fail under a plausible
  lazy implementation.
- Valuation engine: inflation must move when the room overpays; a third QB's
  marginal value must be ~0; spend-down must force full budget use; the
  keeper cost chain must honor +$2, $5 floor, and the 2-year cap; a
  transition-pool test must fail if historical samples are bucketed by
  snapshot years-of-experience.
- Draft engine: team max-possible-bid must equal budget minus $1 per
  remaining open slot; nominated-player analysis must exclude teams with
  full rosters at that position.
- Replay backtest: 2025 draft replayed end to end; running price estimates
  compared against the 180 actual amounts (MAE bound, no systematic drift as
  the pool empties).
- Sleeper client: cache and degradation behavior under simulated endpoint
  failure (no live-network assertions in unit tests).
- Prior art: the keeper EV reference script in the repo is the working model
  the valuation tests formalize.

## Out of Scope

- Placing bids or any Sleeper write operation (API is read-only; automating
  the UI violates Sleeper's terms).
- The standalone multi-year keeper module for trade evaluation and next
  August (v2, post-draft; reuses the NPV code).
- In-season tools: waivers, lineups, trade finders.
- Multi-league generality beyond config, user accounts, auth, mobile layout,
  or any hosted deployment — this runs on localhost for one user.
- Alternative projection sources (FantasyPros etc.); the override file is the
  channel for outside judgment in v1.

## Further Notes

- Hard deadline: live Aug 30, 6:15 PM; mock-draft dry run Aug 29.
- After the Aug 24 keeper deadline, re-pull all teams' locked keepers and
  per-slot budgets as ground truth for draft strategy.
- Biggest risk is the two undocumented Sleeper surfaces (projections, live
  nomination metadata); mitigations are the startup cache, degradation to
  documented picks-polling, and replay as offline fallback.
- GAMEPLAN.md in the repo holds the full decision record, including the
  verified keeper selection (Waddle $12, Hampton $35, McCaffrey $57).
