# Research: Auction Tool Landscape (2026-08-23)

Deep sweep (5 researchers + synthesis: Reddit, commercial tools, community
spreadsheets, open source, strategy/methodology; 332 fetches). Compared
against GAMEPLAN.md / PRD.md. v1-worthy items are folded into the PRD.

## Landscape

The space splits three ways. (a) Community spreadsheets (ElBoberto, CSG, Fantasy Six Pack) have sound VBD and inflation math but require manual price entry, which is untenable under this league's 10-second timers. (b) Commercial assistants mostly fail exactly where the plan aims: FantasyPros explicitly does not support auction live sync; RotoWire and Footballguys are manual-entry for auctions; DraftKick and Draft Sharks claim Sleeper auction sync but it is unverified. Only STACKED and DraftExpert Pro credibly live-sync Sleeper auctions, and both use generic values with no keeper economics. (c) A handful of personal open-source projects (Zookerman3's Auction_Draft_Day, FantasyAgent, Rook, Krool) reverse-engineered the same undocumented live-auction metadata the plan depends on, and their code encodes hard-won gotchas the plan currently misses: CDN caching defeats naive 2s polling, stale nomination pointers after each sale, amount arriving as a string, pause states. Nothing anywhere combines keeper-cost rules, multi-year keeper NPV, and a league's own bid history with live sync; nothing backtests against the user's own room. The plan's core loop (poll picks plus metadata, reprice every sale, max-bid formula) is validated by every serious tool; its differentiators are confirmed unbuilt.

## Tool-by-tool verdicts

- **Zookerman3 Auction_Draft_Day (OSS)** — Closest analog: same read-only Sleeper auction instrument-panel design, built for one league, 293 tests. Lacks keeper NPV and league-history pricing. Its polling/nomination/inflation edge cases are the single best reference for our client layer.
- **DraftExpert Pro** — Architecturally identical (3s Sleeper polling, max-bid on screen), which validates our whole approach. Generic values, iOS-only, keeper handling undocumented. Its Fair/Target/MAX framing maps onto our worth/room-price/max-bid trio.
- **STACKED Auction War Room** — Live Sleeper auction sync plus VORT (best-roster-with minus without) proves our marginal-lineup knob is the right ceiling concept. Subscription product, no keeper economics, no league-specific prices.
- **DraftKick** — Polished commercial cousin; its engineering blog documents the Sleeper sync recipe and the best auction UI principles (positional view, max bid plus profit centered at $0). Values are generic; auction-sync depth on Sleeper unverified.
- **FF Auctioneer** — Claims exactly our loop (Sleeper import, auto-logged sales, continuous repricing, opponent max bids) but has zero independent user footprint. Treat as unvetted; nothing to borrow beyond confirmation.
- **FantasyPros Draft Wizard** — Industry default, and a confirmed dead end for us: auction drafts explicitly unsupported by its live assistant, values widely distrusted. Validates building custom instead of adapting.
- **ElBoberto / CSG spreadsheets** — Community-standard math (VBD-to-dollars, starter/bench split, keeper inflation) that our valuation already subsumes, with league-history calibration they lack. Manual entry makes them unusable at 10s timers.
- **Krool FantasyFootballAnalyzer** — Best written reference for Sleeper auction metadata fields (verified live), but its own live nomination sync is unimplemented future work. Use its API_REFERENCE.md as documentation, nothing more.
- **FantasyAgent (OSS)** — Only Python codebase parsing live auction nomination state; confirms our stack works. Its LLM advice layer (15s latency) is a warning, not a model. Copy its stale-nomination grace-window guard.
- **Rook (OSS)** — Documents Sleeper's Phoenix WebSocket draft protocol with real captured fixtures. Heavier than we need; keep as the documented latency fallback if the Aug 29 mock shows polling lag.
- **Draft Sharks Draft War Room** — Markets dynamic 17-indicator auction values with Sleeper sync, but auction-on-Sleeper is unconfirmed even in their own KB and sync-death complaints exist. Their pitch (static sheets stale after first lot) is our thesis.
- **Academic work (Goebbels 2022, WINE 2016, footapp LP)** — Formal versions of our re-optimization: online re-solve after every sale, LP shadow prices as max bids. Our marginal-lineup plus spend-down is the pragmatic 80%; full LP is over-scope for one week.

## Incorporated / recommended

- [S/v1] Cache-bust every poll: Sleeper's CDN serves the draft object with s-maxage=30 and /picks with s-maxage=15, so a naive 2s poll returns identical stale bytes. Append a changing query param per request (rate limit 1000/min leaves ample headroom).
  - Source: Zookerman3 Auction_Draft_Day (measured)
  - Why: Without this the plan's ~2s polling silently degrades to 15-30s staleness, destroying the whole glanceable-under-10s-timer premise. Highest-value single fix the research found.
- [S/v1] Stale-nomination guard: after a sale, metadata.nominated_player_id keeps pointing at the winner at the winning price until the next name goes up. Cross-check the nominee against the sold set from /picks, with a short post-sale grace window.
  - Source: Zookerman3 nomination.ts; FantasyAgent draft_monitor.py
  - Why: Otherwise the top bar shows a already-sold player as live and Ethan prices the wrong lot. Direct correctness bug in the planned nomination view.
- [S/v1] Pick-parsing and pause gotchas: metadata.amount arrives as a STRING; attribute purchases by draft_slot, not picked_by; is_keeper flag exists on picks; Sleeper auto-pauses overnight and freezes timer_end_at (reads as expired) with a paused flag in metadata.
  - Source: Krool API_REFERENCE.md; FantasyAgent; Zookerman3
  - Why: Each is a cheap unit-testable trap that would otherwise surface for the first time on draft night. Fold into sleeper_client.py and models.py tests.
- [S/v1] Settings-differ guard: on connect, diff the live draft's type/teams/budget/slots/timers against the assumptions the value sheet was priced with and banner every mismatch.
  - Source: Zookerman3
  - Why: Catches the commissioner changing settings (or keeper budget_<slot> entries landing late) after values were computed. Cheap insurance on the plan's read-budget_N-at-startup step.
- [S/v1] Off-model sale handling in inflation math: when a sold player has no value in our sheet, count the dollars against team budgets but exclude the sale from the inflation numerator/denominator, else the ratio drifts all night.
  - Source: Zookerman3 inflation.ts
  - Why: The plan's remaining-money/remaining-value per position is exposed to exactly this skew whenever the room buys someone the projection set didn't price.
- [S/v1] Taper live inflation away from the cheap tail: apply the repricing multiplier to value above the $1-5 floor rather than uniformly, since measured keeper-league inflation runs 15-30% overall but ~0% below roughly the 130th player.
  - Source: RotoGraphs/Fantasy Footballers keeper-inflation evidence; Doc's Auction Draft Tool distribution modes
  - Why: Uniform multiplication is the known-wrong default every sheet ships. One-line change in valuation.py that makes late-draft numbers noticeably more honest.
- [M/v1] Tier-depletion flag: gap-based tiers per position with a remaining-in-tier count, and a last-of-tier warning on the nominated player (the last elite at a position reliably sells $5-10 over any inflation model).
  - Source: CBS 'economic box'; Zookerman3 tiers.ts; scarcity-panic evidence (Kelce $80)
  - Why: The plan's dollar values capture scarcity smoothly but never say 'after him, cliff' — the one signal that changes a max bid discontinuously. Values are already computed; tiers are a cheap derivative. Also improves nomination timing.
- [S/v1] Glanceability layer: plain-language BID/PASS verdict line beside the max bid, profit displayed as price-minus-value centered at $0, budgets shown as remaining-not-spent, positional view as the primary table.
  - Source: Zookerman3 verdict.ts; DraftKick auction-assistant design blog
  - Why: Free, tested UI decisions for exactly the 10-second decision window; keeps the dashboard from becoming a wall of numbers. Constrains static/index.html design rather than adding scope.
- [L/v2] WebSocket fallback path: keep Rook's sleeper_resolver_design.md (Phoenix topic draft:<id>, real captured frames) on file as the zero-latency upgrade if the Aug 29 mock shows polling latency hurting under 10s timers.
  - Source: Rook (sdubois777)
  - Why: Contingency only. Polling with cache-busting plus the anti-snipe 10s reset should suffice; do not build this unless the canary says otherwise.

## Deliberately skipped

- Full LP/knapsack re-optimization of the whole remaining roster per sale (ffanalytics, footapp, Goebbels 2022): the marginal-lineup-with/without knob plus the spend-down schedule captures most of it, and Fantasy Draft Coach's creator abandoned auction mode after 3-4 attempts at exactly this. Not tunable in a week.
- WebSocket-first sync (Rook's Phoenix intercept): undocumented protocol, extension riding a logged-in tab, could break silently on draft day. Polling with cache-busting is proven (DraftExpert Pro ships 3s polling) and the 10s anti-snipe reset guarantees a decision window.
- LLM/AI advice layer (FantasyAgent, FantasyPros Coach AI, nfl_mcp): 15s response times against 10s timers. Precomputed numbers on screen beat generated prose; the plan's design is already correct here.
- Chrome-extension DOM scraping (FantasyPros/RotoWire/Draft Sharks pattern): the documented failure mode of the whole commercial tier ('waiting for draft sync', mid-draft sync death). The public read API is the reliable path.
- Proxy/auto-bidding and any write path (CouchManagers, STACKED limit bids): API is read-only, UI automation violates Sleeper ToS, and read-only is already a stated plan principle.
- Blending external projection sources (FantasyPros consensus, AAV feeds): research shows consensus values are the most-distrusted input in the space; the override CSV is the right channel for outside judgment in v1.
- Mock-auction simulator with AI bidder opponents (chairbender, Draft Dominator): replaying the league's real 2025 draft is strictly better validation for this room; scripted opponents add code, not confidence.
- Nomination-order market model (players nominated ahead of positional rank sell over AAV 75% of the time): interesting but not calibratable on a 12-team league's sample in a week; the planned drain-vs-target nomination suggestions already use the load-bearing inputs (needs, budgets, our targets).
- Multi-device/hosted/Firebase sync and mobile layout: localhost on a second monitor for one user is the plan and is right.

## Confirmed edge (unbuilt anywhere public)

- Keeper economics inside a live auction assistant: research's explicit conclusion — 'no project found that combines keeper-cost rules (price+premium, multi-year caps) with live auction assistance; that intersection is genuinely unbuilt'.
- Pricing from the league's own bid archive: community consensus is that no reliable public historical auction dataset exists and your league's history is the edge. No tool ships empirical position/ADP band medians of the room's actual 2023-25 bids; everything else uses generic projections or platform AAV.
- Multi-year keeper NPV (gamma-discounted keep-option value) embedded in the live bid number: nothing commercial or open-source prices forward-year keeper options into bids.
- Per-position live inflation, formalized: every source acknowledges tier/position-specific inflation and none formalizes it; shipped tools do a single global ratio at best. The plan's per-position remaining-money/remaining-value recomputed each pick is beyond the field (with the tail-taper fix above).
- Backtest against the user's own league: no tool validates its values by replaying the room's actual prior draft with an MAE bound. The 2025 replay is both a unique quality gate and the offline fallback.
- Spend-down schedule (max bid shaded below value early, drifting above late): absent from every shipped tool; only academic work does adaptive pacing.
- Keeper-aware budgets by construction: reading draft.settings.budget_<slot> plus removing kept players from the pool handles keeper inflation natively, where nearly every commercial tool leaves keeper handling undocumented.
