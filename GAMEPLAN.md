# GAMEPLAN — Draft Bot for 12th Week Campers 2026

Agreed 2026-08-23 (grill-me session). Draft: **Sun Aug 30, 6:15 PM**. Keeper
deadline: **Aug 24**.

Pipeline: this file -> `/anthropic-skills:to-prd` -> `/anthropic-skills:to-issues`
-> `/command-center` coding loop in a new environment.

## Keeper decision (Ethan acts by Aug 24, in Sleeper — done outside the bot)

Max-EV combination (multi-year EV, gamma 0.8, adversarially verified by three
independent review agents on 2026-08-23): keep **Jaylen Waddle ($12, EV ~+16)**,
**Omarion Hampton ($35, EV ~+25)**, **Christian McCaffrey ($57, EV ~+5, range
+2 to +8)**. Remove Jayden Reed (ineligible: kept 2024 and 2025, two-year
window exhausted), Lamar Jackson (EV ~-4.5, final keep year), DeVonta Smith
(EV ~-1, final keep year). No $5 bench keep is positive once deep-position
price bias is corrected (the room pays $1-3 there). Ethan enters the auction
with **$96 for 12 roster spots**; if he opts out of the marginal McCaffrey
keep, $153 for 13 spots.

EV = this-year savings + 0.8*E[keep-again option at cost+2] + 0.64*year-2
tail. Room prices from empirical position/ADP band medians of the league's
own 2023-25 bids (log-curve fallback); option terms from year-over-year price
transitions with as-of-season experience buckets and an age-matched pool for
29+ RBs. Method + code: scratchpad keeper_ev2.py (to be re-homed into
draftbot/keeper.py in v2).

## League facts (verified against the API and league sheets)

- league_id `1389692302259138560`, draft_id `1389692302259138561`
- 12 teams, $200 auction, half-PPR, 4-pt pass TD; 10-second nomination AND
  bid timers (the tool must be glanceable — the decisive number already on
  screen when a player is nominated)
- Roster: QB, 2RB, 2WR, TE, FLEX, K, DEF, 6 BN, 2 IR (15 drafted)
- Keeper rule: cost = prior year's price + $2 (min $5), max 3 keepers, max 2
  consecutive keep years. Keepers never appear in the picks feed; each team's
  budget is reduced via `draft.settings.budget_<slot>` once the commissioner
  enters keepers.
- Keeper source of truth: the league roster sheet (Google Sheets
  `1ZJ5gNFbNHUmDiqUiR8b78WBq9ODnAePlcC5CPirMitg`) and khartman95's
  `auction_prices.xlsx` on Drive (3 years of winning bids + K flags).
- Data endpoints (all read-only, keep well under 1000 calls/min):
  - documented: `/v1/league/...`, `/v1/draft/<id>` (+`/picks`),
    `/v1/players/nfl` (~14 MB, cache once daily)
  - undocumented but verified live 2026-08-23:
    `https://api.sleeper.com/projections/nfl/<yr>?season_type=regular&position[]=...`
    returns season `pts_half_ppr` + `adp_half_ppr`; 2023-2026 all retained;
    same player IDs as the rest of the API
  - live auction state: draft object `metadata.nominated_player_id`,
    `metadata.highest_offer`, `metadata.offering_user_id`,
    `metadata.nominating_slot`
- The API cannot bid. This is a decision aid on a second monitor, not an
  autobidder.
- Historical drafts for fitting/backtest: 2025 draft `1257407146123857920`,
  2024 `1124814732579045377`, 2023 `992214922614067201` (12 x 15 picks each
  in 2024/25, 12 x 12 in 2023, winning bid in pick `metadata.amount`).

## Bid policy (six knobs, all signed off)

1. Show **both** numbers: "worth to you" (projection VBD) and "room will pay"
   (empirical median of the league's own 2023-25 bids in a position/ADP band,
   falling back to the log curve `a_pos + b_pos*ln(ADP)` only when the band
   has <6 samples — verification showed the raw curve overprices top RBs by
   $3-6 and deep QBs by $4-8).
2. **Positional inflation**: remaining money / remaining value per position,
   recomputed every pick.
3. **Marginal roster need**: value = best lineup with the player minus best
   lineup without him (a 3rd RB is bench value; scarce QB gets a bump).
4. **Multi-year keeper NPV, gamma = 0.8**:
   `bid = now_value + 0.8*E[max(0, V1-(p+2))] + 0.64*E[max(0, V2-(p+4))]`,
   where the V distributions come from the league's own year-to-year price
   transitions (2023->24->25->26, ~500 samples). PITFALL (verified 2026-08-23):
   the projections API embeds CURRENT years_exp even in historical files —
   always recompute experience as-of-season (`exp_t = exp_now - (2026 - t)`),
   and use an age-matched comparable pool for RBs with 8+ years. Measured
   one-year option value after the fix: cheap young players ~$5-7, veterans
   ~$1-3, market-priced stars ~$0. The $5 floor and +$2 ratchet sit on the
   cost side; projection aging sits in the V distribution.
5. **Bargain early, spend down late**: max bid starts under value and drifts
   to and above it as remaining-money-per-open-slot outpaces the remaining
   pool. Never finish with cash.
6. **Team shape: one star + youth.** Target one ~$50 star, one ~$30 player,
   QB ~$15 (this room sells QBs cheap: Hurts $29, Daniels $13, Mahomes $8 in
   2025), bench filled with $4-10 young players as keeper options.

## v1 scope (must ship by Aug 30)

- Core dashboard: live nominated player + current high bid + my max bid;
  dynamic dollar values for all remaining players; every team's budget, open
  slots, positional needs, and max possible bid; my roster + plan.
- Manual override CSV (personal tiers, avoid list, +/- dollar tweaks) applied
  on top of the model.
- Budget allocation plan that updates as Ethan buys.
- Nomination suggestions (drain-their-budget picks vs cheap targets).
- Replay of the 2025 draft as backtest and offline demo mode.

Post-draft (v2): standalone multi-year keeper NPV module for trade evaluation
and next August, reusing the draft bot's NPV code.

## Architecture

Python 3.11, conda env `draft-bot`. FastAPI backend polls Sleeper every ~2s
and serves one auto-refreshing page (vanilla JS, ~1s refresh). Second monitor.

| Module | Contents |
|---|---|
| `draftbot/sleeper_client.py` | REST wrapper, on-disk cache (players daily, projections, league/draft/picks/rosters), retry, rate-safe polling |
| `draftbot/models.py` | League config, draft state (picks, budgets, rosters, live nomination), keeper costs |
| `draftbot/valuation.py` | Price curve; VBD + marginal lineup; transition/NPV option model; positional inflation; spend-down schedule; override CSV |
| `draftbot/draft_engine.py` | Per-team budgets/needs/max-bids; nominated-player analysis; allocation plan; nomination suggestions |
| `draftbot/server.py` + `static/index.html` | `/state` JSON + dashboard page |
| `draftbot/replay.py` | Replay historical drafts through the engine (backtest + offline fallback) |
| `draftbot/keeper.py` | Post-draft: multi-year NPV keeper evaluator |

Tooling per Ethan's standard: pytest, black, isort, pylint at 10.00.

## Verification

1. pytest with anti-cheat traps: inflation must react when the room overpays;
   a 3rd QB's marginal value ~0; spend-down forces full budget use; keeper
   cost chain honors +$2, $5 floor, 2-year cap; a transition-pool test must
   fail if historical samples are bucketed by snapshot years_exp.
2. Backtest: replay the 2025 draft; running price estimates vs the 180 actual
   amounts (MAE, no systematic drift as the pool empties).
3. Aug 29: live dry run against a Sleeper mock auction draft Ethan creates.
4. Draft day: refresh projections/ADP, read `budget_N` once the commissioner
   enters keepers, cache everything at startup.

## Timeline

- Aug 23: this gameplan -> PRD -> issues; command-center environment set up
- Aug 24: keeper deadline (Ethan, in Sleeper). Coding loop starts: data layer
  + valuation core
- Aug 25-26: NPV/transition model, inflation, draft engine, replay backtest
- Aug 27-28: dashboard, overrides, allocation plan, nominations
- Aug 29: mock-draft dry run, tuning
- Aug 30: live at 6:15 PM

## Biggest risk

The undocumented projections endpoint or live-nomination metadata changes or
lags on draft day. Mitigation: cache all values at startup (dashboard still
prices everyone), degrade to documented picks-polling (budgets and rosters
stay exact; nomination view goes manual), `replay.py` is the offline
fallback. The Aug 29 mock draft is the canary.
