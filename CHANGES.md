# V8.1 - "no signal" hardening + pipeline doctor

Why a scan could finish with zero alerts even though the rule logic itself was fine (the synthetic end-to-end test
produces CALL and PUT alerts on a clean trend, so the rule is not what starves the bot). Four things could silently
discard good setups on a live feed; each is fixed and covered by `tests/test_v81_hardening.py`.

1. **Just-closed candle not published yet.** The scan runs 8 s after the 5-minute boundary. If Upstox had not published
   the new candle by then, the newest candle the scanner saw was 5 minutes old, the symbol was dropped as "stale spot
   candles", and that candle was never evaluated (the next scan looks at the next candle). The scanner now re-fetches
   up to `CANDLE_READY_RETRIES` (4) times, `CANDLE_READY_WAIT_SEC` (3 s) apart, never later than
   `CANDLE_READY_MAX_WAIT_SEC` (45 s) after the boundary. The wait is shared by all workers, so a slow feed costs one
   short wait per scan, not one per symbol. `CANDLE_READY_RETRIES=0` restores the old behaviour.
2. **Missing history = blind until about 12:30.** If the 5m history download failed at bootstrap (HTTP 429, token or
   network), indicators needed 40 candles of today alone. The scanner now backfills history for short symbols during the
   scan (`HISTORY_BACKFILL_*`: at most 25 requests per scan, 3 attempts per symbol per day), and the SIMPLE history
   requirement is 35 candles (ADX needs 29 + the freshness window) instead of 40. The bootstrap "short history" check
   and cache check now use the same number in SIMPLE mode (they used the legacy 50, flagging healthy symbols).
3. **One exception cancelled the whole scan.** Stage 3 evaluated candidates in a bare loop: one odd option quote or DB
   hiccup raised out of `run_once`, the candidates behind it never got an alert, and because the candle was not marked
   as scanned the same crash repeated on every retry until the candle expired. Each candidate (and each symbol's rule
   evaluation in stage 1) is now isolated; failures are counted as `evaluation_errors` and shown in `/status`.
4. **Silence was indistinguishable from "no setups".** A "first scan of the day" Telegram report now arrives right
   after the first scan: usable candles, failed/stale counts, history status, rule triggers, top blocking gates.

New tool `tools/doctor.py` (run on the Render shell or locally with your real variables): checks the token, the
instrument master, candles (including lag and the just-closed candle), history length, the rule on live data (with the
most common blocking gates), then dry-runs option chain, contract pick and risk levels, and optionally sends a Telegram
test (`--telegram`). `--selftest` runs it offline on synthetic data.

Tests: 299 run here (289 original + 10 hardening + 2 doctor - the original suite is unchanged). All pass except 6
that are identical in the untouched repository and come from the build sandbox, not the code: no `pytest` module
(`test_scan_lifecycle`, which passes through a shim) and an ad-hoc runner that skips class-level fixtures
(`test_infra.TestServer`, which passes under `python -m unittest`: 36/36).
Mutation-checked: removing each fix makes its tests fail. NOT tested against live Upstox / Turso / Telegram - there is
no network or credentials in the build environment. Run `python tools/doctor.py` on the deployed service first.

---

# V8.0 - simple signal mode, fewer silent failures

Goal: one rule that is easy to read and tune, and a scanner that says so when it cannot work.

## Signal rule (`strategies/simple_signal.py`, default `STRATEGY_MODE=SIMPLE`)
Evaluated on the latest closed 5-minute candle (first scan 09:30, last 14:45):
- **CALL**: EMA9 > EMA21, close > session VWAP, RSI(14) in 55-75, ADX(14) >= 25.
- **PUT**: EMA9 < EMA21, close < session VWAP, RSI(14) in 25-45, ADX(14) >= 25.
- **Fresh only**: fires while the full condition set has been true for at most 3 candles (`SIMPLE_FRESH_BARS`), so late
  entries in an old trend are skipped, and a failed option-chain/contract check gets two more candles to retry.
- Stop = 1 x 5m ATR on the underlying, targets 1.5 / 2.0 ATR (`SL_ATR`, `T1_ATR`, `T2_ATR`). No MACD, DI gap, candle-body,
  extension, breakout/pullback or score gates.
- Tune by env var: `SIMPLE_ADX_MIN`, `SIMPLE_RSI_CALL_MIN/MAX`, `SIMPLE_RSI_PUT_MIN/MAX`, `SIMPLE_FRESH_BARS`.
- `STRATEGY_MODE=LEGACY` restores the old Normal Trend / MEX / ORB engines (and `LIVE_INTRABAR_ENABLED=true` their intrabar path).

## Alert path (what happens after the rule fires)
- **Stale option chain**: chains are prefetched for every candidate and then evaluated one by one (each with remote DB writes),
  so a chain fetched early in the batch could pass 60 s before its turn and the signal was dropped as "stale option chain".
  The scanner now fetches one fresh snapshot instead; it rejects only if that one is also stale.
- **Drift limits** between candle close and alert: adverse 0.40 -> 0.50 ATR, runaway 0.60 -> 0.80 ATR (`LIVE_MAX_ADVERSE_ATR`,
  `LIVE_MAX_RUNAWAY_ATR`). The spot is read 30-90 s after the candle closes on a REST scan.
- **Chain-quality check** now looks at ATM +/-2 strikes and vetoes when more than 40% of them lack a bid or ask (was ATM +/-3
  and 20%, which one-sided far strikes of stock options trip routinely). The picker still demands a two-sided quote on the
  strike it chooses.
- Intrabar (websocket partial-candle) detection is off unless `LIVE_INTRABAR_ENABLED=true` in LEGACY mode.

## Silence is no longer a mystery
- Telegram "DATA PROBLEM" when >= 50% of the universe had no usable candles in a scan (failed / stale / empty), with the last
  provider error; at most once per 30 minutes (`DATA_HEALTH_ALERT_FRAC`, `DATA_HEALTH_ALERT_COOLDOWN_SEC`).
- Telegram alert when the daily bootstrap fails (`Daily bootstrap failed`) and when `Bot()` cannot even be constructed
  (`F&O bot failed to start`, e.g. missing Turso variables). Previously these only appeared in the Render log.
- The existing quiet-scan digest now names the simple engine and the gates that blocked it (e.g. `CALL:adx x 40`).

## Tests
- 327 pass (305 original + 22 new in `tests/test_simple_signal.py`). `tests/conftest.py` pins the LEGACY settings for the older
  tests; `test_simple*.py` files run against the shipped defaults. Every new behaviour was mutation-checked (the test fails
  when the behaviour is removed).
- Not tested against live Upstox/Turso/Telegram: there is no network or credentials here. Synthetic data shows how selective the
  rule is, not whether it is profitable. Paper-trade it (PAPER_MODE=true) and check `/report` before trusting any number.

---

# Changes — accuracy-fix-4

- Consolidated numerical approval to one final alert score gate: `ALERT_SCORE_MIN=8.0/10` by default.
- Removed the separate 0–100 candidate price-score threshold; setups enter candidate evaluation based on a real strategy vote.
- Removed futures OI / option-chain directional agreement and opposing-OI vetoes as alert-approval gates. OI is not required to agree with the price setup.
- Removed strategy-specific score floors; every strategy uses the same final score threshold.
- Higher-timeframe trend is scoring context, not a separate rejection gate.
- Kept essential safeguards: closed/fresh candles, valid option chain and executable quotes, strategy technical quality checks, live-price reversal/chase checks, risk/reward, market hours, duplicate prevention and cooldowns. Option-chain data remains necessary to select an actual option contract; its directional OI is not an approval gate.
- Kept signal-only behavior; no order execution was added.

# Fix: bot sent only the startup message and never a signal

Root causes found in the code:
1. **₹500 risk rule** (`RISK_CAPITAL 100000 x 0.5%`): every signal whose one-lot stop loss risked more than ₹500 was rejected
   ("one-lot risk exceeds trade budget"). With NIFTY lot 75 or stock lots of 500+, that is almost every option. Removed
   completely (config, sizing, daily-loss/open-risk caps, Telegram text).
2. **~15 hard filters stacked** (ADX, EMA separation, <=2 EMA flip-flops, 2 VWAP closes, RSI rising, strong close, fresh cross
   or pullback ... plus *mandatory* futures-OI, 15m trend, OI wall, spread 1.5%, OI/volume lots). Any one failing = no signal.
3. **Silent warm-up failure**: if 5m history failed to load, indicators needed 40 bars (first usable ~12:30). Now 30 bars,
   and a Telegram warning is sent at startup when history is missing for many symbols.

What changed:
- New score-based strategy (`strategies/price.py`): EMA 9/21 (25) + VWAP (15) + MACD (15) + RSI (15) + ADX (15) + Volume (10)
  + ATR (5) = 0-100. Five hard gates (EMA side, VWAP side, MACD histogram side, RSI not exhausted, not over-extended, ATR floor)
  plus one fresh trigger (EMA cross / MACD cross / N-candle breakout / pullback-resume). FULL signal at score >= `PRICE_MIN_SCORE` (72).
- Futures-OI / option chain / regime now add or remove a few points; only an extreme opposing reading vetoes. 15m-trend and
  OI-wall checks are soft by default (`HTF_REJECT_OPPOSED`, `WALL_VETO`).
- Liquidity filters relaxed (spread 3%, a feed with no bid/ask is allowed), first signal 09:30, last 14:45, caps raised.
- `/ping` now keeps Render awake **and** runs one bot step. Point cron-job.org at it every minute.
- Telegram digest every 6 quiet scans explains *why* nothing fired; `/status -> last_scan.no_setup_reasons` has the same data.
- **Reversal module** (`REVERSAL_ENABLED`): counter-trend Call/Put when price was stretched >= 2 ATR beyond EMA21, RSI hit an
  extreme (<=32 / >=68) and is turning, MACD histogram turns, and a strong reversal candle breaks the previous candle. Scored with
  volume spike, RSI divergence, EMA9 reclaim. Stop sits beyond the rejected swing low/high. Alerts are labelled REVERSAL.
- **EARLY tier** (`EARLY_ENABLED`): scores 60-71 are sent too, labelled "EARLY - lower confidence", max `EARLY_DAILY_CAP` (6) per day.
- Tests: 88 pass (old price-gate/scorer tests replaced by `tests/test_strategy.py`). Not tested against live brokers.

# Accuracy update: 5 signal types, self-checking bar, performance report

- **Signal types** (all from EMA 9/21, VWAP, MACD, RSI, ADX, Volume, ATR; every alert is labelled with its type):
  TREND (EMA/MACD cross, breakout, pullback-resume), REVERSAL, ORB (first-15-min range breakout), VWAP (reclaim for Calls /
  rejection for Puts), SQUEEZE (ATR contraction then expansion breakout). Each can be switched off in the env.
- **Confluence**: when two types agree on the same side the score gets a bonus and the alert lists them; when the best
  Call and Put setups are within 10 points of each other the symbol is skipped as ambiguous.
- **Anti-chase / wick penalty**: a signal candle stretched >1.2 ATR from EMA9, or with a rejection wick, loses points.
- **New gates that remove noisy setups without just raising the score bar**: ADX must show a trend for TREND signals;
  VWAP crosses and reversals need volume expansion (when volume data exists); ORB only fires on the first breakout.
- **Adaptive bar** (`engine/adaptive.py`): from the last 30 days of tracked signals, each type (and the EARLY tier) needs
  extra confidence if its profit factor is weak (+6 / +12), and gets -3 if proven (PF >= 1.5). Needs 12 closed signals first.
- **Best first**: at most `MAX_SIGNALS_PER_SCAN` (3) signals per 5-minute scan, highest score first.
- **Report**: `/report?key=SECRET&days=30[&send=1]` and a compact per-type table in the daily summary: win %, profit factor,
  average P&L per lot by type, tier, score band, hour, side, index vs stock.
- Tests: 101 pass. Backtest on real data was not possible here: use paper mode and the report to tune.

---


# Rebuild: EMA 9/21 + VWAP + RSI + ADX strategy

The price layer was rebuilt on exactly four indicators. ORB, 20-bar breakout-volume, the EMA20/EMA50 pullback, the
efficiency-ratio trend gate and the (unused) supertrend are gone. Reasoned changes, not calibrated ones: paper-trade
before trusting any number.

## Strategy (`strategies/price.py`)
- One setup engine, two triggers: `EMA_CROSS` (EMA9 crossed EMA21 in the last 3 candles) and `EMA_PULLBACK` (trend resumes after a dip to EMA9 that held EMA21).
- Gates (all must pass): ADX >= 20 or building, +DI/-DI lead >= 3; EMA9/21 separation >= 0.10 ATR, EMA9 rising, <= 2 EMA flip-flops in 20 candles; two closes on the right side of VWAP, VWAP not leaning against the trade, <= 2 VWAP crosses, within 2 ATR of VWAP; RSI 52-72 rising (Put 28-48 falling); close within 2 ATR of EMA21; trigger candle goes the trade's way, closes beyond EMA9 and near its extreme.
- Each alert's first reason line shows the live EMA9/EMA21/VWAP/RSI/ADX values.
- `MIN_SESSION_BARS = 6`, `FIRST_SIGNAL_TIME` 09:30 -> 09:45: VWAP needs a few closed candles before it means anything.

## Optimisations made while rebuilding
- **Scorer recalibrated** (`engine/scorer.py`, `config.py`). The old confidence divided by the sum of all four weights, so with no chain/regime evidence (the normal case) a signal needed ~86% on both price and futures-OI just to reach 60: price 0.73 + OI 0.72 scored 51 and was rejected. Confidence is now the weighted blend of the two *mandatory* sources (price + OI) +/- `CHAIN_BONUS` (15) / `REGIME_BONUS` (10) points, then the same multiplicative penalties as before. The same setup scores 72.6. `MIN_CONFIDENCE` stays 60, so roughly "price >= 0.6 and OI >= 0.6" is the new bar. Side is now decided by the price source (OI must agree) instead of the sign of a four-way sum.
- **Real VWAP for indices** (`engine/scanner.py`, `indicators/core.py:with_volume`). Index spot candles have no volume, so VWAP was a time-weighted mean. Index spot typical price is now weighted by current-month futures volume (no futures-basis leakage). Falls back to the time-weighted mean when less than 80% of today's bars have futures volume (`/status` counts these). Switch off with `INDEX_VWAP_FROM_FUTURES=false`. Closes the "Index ORB/breakout volume" item listed under *Still open* below.
- **No duplicate futures call**: the futures candles fetched in stage 1 for index VWAP are reused for the OI build-up in stage 2.
- **Structural stop** (`engine/risk_rules.py`, `strategies/price.py:invalidation_distance`). Underlying stop sits beyond the nearer of EMA21 / VWAP plus 0.25 ATR, clamped to 0.7-1.3 ATR (`SL_MIN_ATR`, `SL_MAX_ATR`). Targets are at least 1.5R / 2.5R of that stop (`T1_R`, `T2_R`) so a wide stop cannot silently shrink R:R. Without a structural stop the old 1 ATR / 1.5 ATR / 2.5 ATR levels apply.
- **Regime chop** now comes from ADX (< `ADX_RISING_FLOOR`) instead of the efficiency ratio; the efficiency-ratio, supertrend and opening-range helpers were removed from `indicators/core.py`.

## Tests
- 93 tests pass. Every price gate has a test that fails when that gate is removed (checked by mutation, one gate at a time). New tests: EMA-cross and pullback fixtures for Call and Put, scorer scale, structural stop clamps, futures-volume VWAP and its fallback, stage-1 -> stage-2 futures reuse.
- Fixture fix: the e2e gap hack set the first candle's open outside its high/low, so the scanner's data guard silently dropped that candle. Removed.

## Behaviour to expect / limits
- A gap-open day keeps price far above EMA21, so the anti-chase gate stays quiet until the EMAs catch up (usually the first hour). That is intended, but it means fewer early signals on big-gap days. Relax `MAX_EXTENSION_ATR` only with paper evidence.
- `EMA_CROSS` is a lagging trigger and fires rarely (it needs a steady move, not a spike); `EMA_PULLBACK` is the workhorse.
- On synthetic random-walk sessions the price layer alone fires on roughly 0.2-0.35 scans per symbol per day, before the OI / chain / 15m / risk filters. That is a sanity check on selectivity, not an estimate of edge.
- Not tested against live brokers (no network here).

---

# Signal-logic changes: fewer false signals, less lag

Goal: fewer false signals, and a signal that is placed at the moment the setup is still valid and priced at the executable quote.
These are reasoned changes, not calibrated ones. Verify on paper trades (or a replay) before trusting the numbers.

## Fewer false signals
- OI_CONFIRM_MIN (0.30): futures OI must agree with the price signal. It is now mandatory, not just a weighted vote.
  Chain/PCR evidence alone can no longer carry a trade.
- MIN_CONFIDENCE 55 -> 60 (midday and late stay +5, so 65 there).
- ADX_MIN 18 -> 22, ADX_RISING_FLOOR 13 -> 16: trend must be real, not building chop.
- CHOP_ER_HARD 0.15 -> 0.22: efficiency ratio gate tightened.
- BODY_MIN 0.50 -> 0.55, CLOSE_POS_MIN 0.65 -> 0.70: trigger candle must be decisive.
- MAX_SPREAD_PCT 2.5 -> 1.5: wide-spread options produce fake R:R.

## Less lag, pinpoint entry
- ORB_FRESH_BARS 3 -> 2: breakout must be at most 2 candles old.
- MAX_EXTENSION_ATR 2.5 -> 2.0: skip entries already stretched from EMA20.
- LIVE_MAX_ADVERSE_ATR 0.35 -> 0.20, LIVE_MAX_RUNAWAY_ATR 0.80 -> 0.30: if price has moved since the candle closed, skip.
- ENTRY_BAND_DOWN 0.5% -> 0.2%, ENTRY_BAND_UP 1.0% -> 0.4%: entry window is tight around the reference price.
- VALID_MINUTES 15 -> 10: a stale signal expires sooner.
- Entry, R:R, stops and cost are measured from the ASK (fallback LTP), since that is what a buyer pays.

## Kept on purpose
- SCAN_DELAY_SEC = 8: the lag floor. Shortening it risks reading a candle that is not final yet.

## Code
- scorer.py: mandatory futures-OI check.
- scanner.py: executable price (ask) used for entry, levels and cost.
- strategies/structure.py removed: it was never imported and needed modules not in this repo.
- tests: fixture breakout in trend_scenario reduced from 6 to 4 points, because the old 6-point breakout was 2.1 ATR from EMA20 and is exactly the late entry the new gate skips. Two scorer tests updated to the 60 threshold. One regression test added for mandatory OI confirmation. 76/76 pass.

## Still open (not done here)
- Index ORB/breakout volume: index spot has no volume, so volume confirmation is skipped for NIFTY/BANKNIFTY. Using current-month future volume would fix it.
- Exits: underlying targets (und_t1/und_t2) are alerted but not tracked; premium levels ignore gamma.
- Backtest/replay harness to calibrate thresholds per setup.


## Follow-up: signal availability and diagnostics
- Reduced a few overly restrictive default gates modestly (ADX 22->20 and efficiency ratio 0.22->0.18 (core confidence and mandatory futures-OI confirmation remain unchanged)) to allow more qualified setups without removing the OI, trend, contract, or risk checks. Validate with paper trading before live use.
- Increased stage-1 shortlist from 10 to 15 so more candidates can reach confirmation.
- Removed the default 60-minute per-symbol cooldown; active-signal, daily-loss, daily-cap, correlation and quality checks remain. Set `SYMBOL_COOLDOWN_MIN` if you want an optional cooldown.
- Added `/status` scan diagnostics for empty provider responses, missing closed candles, no price setup, stage-1 threshold misses, and excluded symbols. This should make “no signal” causes visible instead of silently returning an empty list.
- Existing token management is retained and refreshed runtime tokens are persisted in the configured DB across service restarts: Dhan can auto-generate/refresh when client ID + PIN + TOTP secret are configured; Upstox Analytics tokens are long-lived but must be replaced manually on expiry, while Upstox daily token requests require user approval in the Upstox app/WhatsApp. No broker authentication flow can guarantee unattended renewal if the broker requires user approval.


## Risk-control layer (manual Telegram alerts)
- Adds configurable capital (default ₹100,000), 0.5% per-trade risk budget (₹500), 1.5% modeled daily loss threshold (₹1,500), and 1.0% modeled open-risk cap (₹1,000).
- Sizes in whole lots using estimated premium stop-loss risk. If one lot exceeds ₹500, the candidate is rejected rather than loosening confirmation filters.
- Telegram alerts include estimated risk per lot, suggested maximum lots, and explicit manual-trading caveats. No broker orders are placed.
- Daily stop/open-risk controls use the bot's tracked signal outcomes and suggested lots. They cannot know actual manual fills, quantity, fees or user P&L; users must independently stop trading at their real account limit.
- Existing price/OI/chain/regime confirmation checks and auth/token renewal paths are retained.


## Indicator and scanner-quality optimization pass
- Corrected ATR to seed Wilder smoothing with the first complete N-bar true-range average; the old implementation recursively smoothed from candle one, biasing ATR and every ATR-normalized threshold.
- Added OHLC validation, closed-bar filtering, chronological ordering and duplicate-timestamp removal before candles reach indicators.
- Added a configurable stale-feed guard (`MAX_CANDLE_LAG_SEC`, default 180 seconds) so the scanner does not emit signals from an old last candle after a provider stalls.
- Changed ORB and breakout-volume baselines from the mean to the median of prior positive-volume candles, reducing sensitivity to one-off volume spikes. Index candles without volume still do not gain fake volume confirmation.
- Added regression tests for ATR seeding, invalid ATR periods, median relative volume, candle validation/deduplication and stale feeds.
- Existing confidence threshold, futures-OI confirmation, HTF checks, option-chain/liquidity checks, risk sizing, and token renewal are preserved. The corrected ATR can legitimately change which setups qualify; thresholds were not lowered to force more alerts.


## Accuracy fix 2 — Momentum Explosion integration

- Momentum Explosion detection now runs independently of which strategy wins the top price-vote ranking. A higher-scoring trend/ORB setup no longer prevents a valid MEX candidate from entering its confirmation lifecycle.
- Waiting for the next-candle MEX confirmation no longer clears an unrelated valid price setup for the same underlying.
- The detected MEX score and explanation are persisted with the candidate and reused when the next closed candle confirms the breakout; the confirmation candle does not have to qualify as a second explosion candle. Opposite-direction conflicts are still rejected.
- Relaxed the MEX-specific RSI exhaustion gate: RSI may exceed the ordinary trend-entry band during a genuine burst; the independent ATR extension/chase gate remains active, and only extreme RSI readings (>=95 for CALL / <=5 for PUT) are blocked by this check.
- Added regression tests for preserved lifecycle score/reason and a realistic breakout with high RSI.
- Validation: `python -m compileall -q .`; `python -m unittest discover -s tests -v` — 159 tests passed. These are local software checks, not proof of live Upstox data quality or profitability.


## accuracy-fix-3
- MEX candidates now expire before any out-of-window breakout can confirm them.
- The scanner emits a Momentum Explosion vote only on the actual confirmation candle, rather than repeatedly treating the still-live confirmed lifecycle as a fresh entry trigger.

## Accuracy fix 5 — higher-timeframe trend quality

- Upgraded `htf_bias()` so 15-minute EMA9/EMA21 structure and slope are confirmed by 15-minute ADX(14) >= 18 and directional +DI/-DI agreement.
- Weak or mixed 15-minute conditions now return neutral context rather than being mislabelled as an opposing trend.
- The 15-minute result remains score context only; it does not independently block an otherwise valid signal. Existing completed-candle, quote freshness, contract validity, risk, and duplicate protections remain in place.
- Validation: `python -m pytest -q` — 160 passed, 4 subtests passed.

## V3.0 — NORMAL + ORB + MEX redesign profile

- Added `STRATEGY_MODE=V3` / `V3_PROFILE_ENABLED=true` to select the three independent engines while preserving SIMPLE rollback mode.
- NORMAL universe set to the requested 50 fixed stocks (indices remain supported separately); V3 thresholds use ADX 25, RSI 50 directional thresholds, DI direction, RVOL 1.2 when available, and 1.5 ATR extension allowance.
- NORMAL MACD is now optional diagnostics, not a hard dependency or veto. Candle-body strength remains diagnostic; structural price triggers decide setup validity.
- ORB defaults aligned to 0.65 ATR clearance, 55% body and 75% close-position; retest remains optional.
- MEX defaults aligned to ADX 18, 0.65 ATR impulse and 1.8x range expansion; ADX is checked in both closed-bar and live partial-bar paths. RVOL remains supporting context.
- Added premium and underlying TP3 levels at 4R; Telegram now displays TP1/TP2/TP3. Existing lifecycle tracker remains TP1/TP2 based (TP3 is not yet an independently tracked exit state).
- V3 Render blueprint uses a 7.0/10 score floor, 0.45–0.65 option delta range, 1.5R minimum T1 reward/risk, and PAPER_MODE=true.
- Added `V3.0-REDESIGN.md` with rollout settings and explicit validation limitations.
