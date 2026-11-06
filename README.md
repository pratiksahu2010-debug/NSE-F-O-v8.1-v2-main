# NSE F&O Signal Engine — V3.0 rollout

The repository now includes the V3 NORMAL + ORB + MEX profile. The Render blueprint selects
`STRATEGY_MODE=V3`, `V3_PROFILE_ENABLED=true`, and a 7.0/10 alert floor while keeping
`PAPER_MODE=true`. The prior SIMPLE mode remains available for rollback. See
[`V3.0-REDESIGN.md`](V3.0-REDESIGN.md) for exact rules, environment settings, limitations and
validation requirements.

**Signal-only:** this project does not place broker orders. Do not infer profitability from
unit tests or underlying-only replay.

---

NSE F&O Signal Engine V8.1 (simple mode)

## Balanced Hybrid V2 engine — staged rollout

The rebuild is available on the `rebuild/balanced-hybrid-v1` branch and is integrated
behind a safety flag. The production default remains the existing scanner:

```env
V2_ENGINE_ENABLED=false
V2_MIN_SCORE=65
V2_MIN_RR=1.5
V2_MIN_HISTORY_BARS=220
# V2 ADX(14) minimum is fixed in code at 22; it cannot be lowered by env
```

Set `V2_ENGINE_ENABLED=true` only after reviewing replay results and completing paper
validation. This is still a signal-only system; it does not submit broker orders.

The V2 engine evaluates nine independent 5-minute setup families:

1. Big bullish/bearish momentum
2. Opening-range breakout (first three completed 5-minute candles)
3. Volume-confirmed breakout/breakdown
4. Breakout retest
5. Trend pullback/resumption
6. VWAP reclaim/rejection
7. Momentum reversal with price-structure trigger
8. Volatility squeeze breakout
9. Failed breakout/liquidity sweep

A shared 100-point score combines setup/price structure, EMA alignment, VWAP, ADX/+DI/-DI,
relative volume, RSI/MACD, completed 15-minute context, candle quality and volatility.
ADX(14) >= 22 is a hard publication gate for every V2 strategy and both early previews
and closed-bar confirmations. Missing ADX is rejected. A score cannot override this gate.
Entries more than 1.25 ATR from EMA21 are rejected as overextended; the setting is
available as EngineConfig.max_entry_extension_atr.
Provisional grade bands are A+ (85-100), A (75-84), B (65-74); below 65 is rejected.
These are initial engineering thresholds, not evidence of profitability.

The context layer classifies TRENDING_UP, TRENDING_DOWN, COMPRESSION, SIDEWAYS,
CHOPPY and VOLATILITY_EXPANSION regimes. The impulse detector scores range expansion,
relative-volume expansion, ADX acceleration, EMA separation and MACD histogram expansion.
It adds a separate NORMAL / STRONG / EXPLOSIVE move-potential label; this label is not
a promise of future movement and never creates a signal by itself. Relative strength
versus NIFTY is calculated from timestamp-aligned session returns when both returns
are available; missing reference data is neutral, not fabricated.

The resolver selects one candidate per underlying/candle and rejects unclear opposing
directions. The 220-bar history floor is mandatory. Indicators use finalized candles;
15-minute context is built from completed bars only. For index underlyings, the engine
uses current-session futures volume to weight the index-price VWAP/RVOL only when timestamp
coverage is adequate; otherwise index volume is treated as unavailable rather than
fabricated. Volume-confirmed breakout requires usable RVOL.

Hybrid entry handling:
- `EARLY INTRABAR` is a provisional Telegram preview. It is not saved as an active signal
  and is not added to the trade tracker.
- `CLOSED-BAR CONFIRMED` is the finalized signal and is persisted/tracked normally.
- If the closed candle changes direction, the alert explicitly says the closed-bar result
  takes priority over the earlier preview.

V2 option selection validates index/stock spread limits, executable bid/ask, one-lot
market depth, OI and volume in lot units, expiry distance, fresh option-chain timestamp,
and a real Greeks delta inside the configured range. Missing delta is rejected; no
assumed 0.5 delta is substituted. Portfolio-wide daily signal, loss, active-position and correlation-group caps are not applied;
strategy quality, per-candle duplicate prevention, contract validation and entry-risk checks remain active.

### V2 historical replay

Use archived 5-minute candles to evaluate the V2 strategies before enabling the feature
flag. Replay is on the underlying, not option premiums:

```bash
python tools/replay_v2.py --db "$DATABASE_URL" --start 2026-09-01 --end 2026-10-31
python tools/replay_v2.py --csv-dir candles/ --symbols NIFTY,RELIANCE --train-frac 0.7 --out v2-replay.csv
```

The replay uses closed candles, the 220-bar warm-up, the unified conflict resolver and conservative stop-first same-candle outcomes. No portfolio-wide
daily signal, loss, active-position or correlation-group caps are applied in replay.
It does not model intrabar tick previews, historical option-chain liquidity/Greeks,
option-premium changes, theta, IV, brokerage or slippage. Underlying R results must not
be interpreted as option-premium profitability.

No signals? Run `python tools/doctor.py` first (Render shell, or locally with the same environment variables). It walks
the pipeline - token, instrument master, candles, rule, option chain, contract, risk, Telegram - and names the stage that
fails. V8.1 also waits for a late-published 5-minute candle, backfills missing history during the scan, isolates per-symbol
errors and sends a "first scan of the day" report to Telegram. Details: CHANGES.md (V8.1).

Default signal rule (`STRATEGY_MODE=SIMPLE`, closed 5-minute candles): CALL when EMA9 > EMA21, close > session VWAP,
RSI(14) 55-75 and ADX(14) >= 25; PUT is the mirror (RSI 25-45). It fires only while the setup is at most 3 candles old.
Everything after the rule (option chain, contract, executable ask, ATR stop/targets, duplicate protection, Telegram) is
unchanged in purpose. See CHANGES.md (V8.0) for the thresholds, env vars and the silent-failure alerts.
`STRATEGY_MODE=LEGACY` restores the V7.5 multi-engine behaviour described below.
Production signal path — independent strategy engines
The current strategy refactor runs three independent signal engines on the fixed Upstox NSE F&O universe. Each engine proposes an underlying setup; a shared execution-safety layer then validates the option contract, fresh executable ask, structural stop/targets, entry freshness, duplicate state and Telegram delivery. The bot remains signal-only and never places broker orders.
Normal Trend (50 fixed stocks plus supported indices): 5-minute EMA 9/21, MACD (12/26/9), session VWAP, RSI (14), ADX (14), and directional +DI/-DI. Three setup paths share one detector: early trend formation (ADX 20-25 only while rising), trend pullback/resumption, and fresh momentum breakout. All paths require directional EMA/VWAP/RSI/MACD/DI confirmation, a strong trigger candle, a six-candle breakout boundary, and a maximum 0.60 ATR distance from the trigger level. The 15-minute chart is informational context only.
Opening Range Breakout (fixed 100-symbol universe): opening range is the first three 5-minute candles (09:15, 09:20 and 09:25). Breakout evaluation starts after 09:30 and is limited to the configured ORB window. Both upside and downside breaks are supported. Clean acceptance beyond the range can trigger immediately; a pullback/retest is preferred when it occurs but is not a compulsory second-candle confirmation.
Momentum Explosion (same fixed 100-symbol universe): a dedicated MEI detector looks for compression release or unusually strong range expansion, a directional MACD turn, a fresh local boundary break and a non-exhausted entry. It can trigger on the detection candle; there is no compulsory next-candle lifecycle. Relative volume is diagnostic when usable, not a hard gate.
Live intrabar path: Upstox WebSocket ticks build a separate partial 5-minute candle. Normal Trend, ORB and MEX can detect a developing move without inserting a partial candle into closed history. Live triggers still require a fresh underlying tick and the shared option/risk checks.
Shared safety checks: current NSE F&O instrument-master eligibility, fresh Upstox feed, current option-chain snapshot, valid CE/PE contract, executable option ask (never LTP substitution), live entry displacement, structural stop, remaining reward/risk, active-signal and persistent duplicate protection. These are execution-integrity controls, not extra strategy indicators.
Universe integrity: ORB and MEX each request the complete static 100-name list. Bootstrap validates membership against the current Upstox NSE_FO instrument master and exposes missing symbols in diagnostics; it does not silently replace them with dynamic turnover-ranked names.
One alert per underlying per scan candle: when multiple engines trigger simultaneously, the routing priority is ORB, then MEX, then Normal Trend. This avoids duplicate/contradictory alerts while leaving each detector independently evaluated.
Legacy V7 sequential and legacy PRICE score components are retained only where needed for compatibility/diagnostics; they are not prerequisites for the three routes above. Automated tests cover indicator warm-up and no-trigger behavior in flat conditions. Unit tests do not establish profitability or guarantee fills.
Indicator definitions
EMA 9/21: recursive EMA, seeded from the first available close.
RSI(14): Wilder RMA of close-to-close gains and losses.
ATR(14): Wilder RMA of true range, including previous-close gaps.
DMI/ADX: 14-period Wilder directional movement and 14-period ADX smoothing; output is ADX, +DI, -DI.
Session VWAP: HLC3 weighted by volume and reset at each IST trading date. When a whole session has zero usable volume, the engine uses a cumulative HLC3 TWAP fallback and explicitly labels it `TWAP_FALLBACK` in diagnostics. For index underlyings, futures volume is attached to spot HLC3 when provider coverage is adequate.
Step Channel (TradingView Pine v6 source supplied by the project owner): ATR 14, SMA(close, 50) initial MID, inner bands +/-2.5 ATR, outer bands +/-4 ATR. On each confirmed close, compare against the previous inner bands first; only then update MID to close and step ATR to current ATR. Recalculate all bands after the state update.
Upstox historical-data resilience
Historical REST requests are globally paced by the shared Upstox limiter (default 1 request/second).
HTTP 429 responses trigger a shared cooldown and circuit breaker across all Upstox REST callers; the request that received 429 gets at most two exponential-backoff retries by default, while queued symbols fail fast during the cooldown. A provider `Retry-After` header is respected.
Historical 5-minute candles are persisted in the PostgreSQL candle archive. On restart, a sufficiently populated recent archive is reused instead of issuing one historical request per symbol again.
If Upstox history refresh fails, the bot falls back to cached candles and reports per-symbol errors and warmup coverage in `/diagnostics.history_bootstrap`.
Symbols without enough history remain visible as short-history cases; no stale or fabricated candles are created. Live intraday freshness and signal/risk validation remain in force.
Optional controls: `UPSTOX_RPS` (default `1.0`), `UPSTOX_429_RETRIES` (default `2`), and `UPSTOX_429_COOLDOWN_SEC` (default `15`).
Diagnostics
The latest `/status` payload includes `v7_sequential` for each scanned symbol:
WAITING / REJECTED / UNDERLYING_SIGNAL_CONFIRMED, setup type (fresh cross or pullback), and exact reason
last completed candle open/close time
EMA9/EMA21 and spread, crossover age
VWAP value and calculation mode
RSI, ADX, +DI, -DI, ATR
Step Channel MID, bands, state and event
Safety and deployment
Signal-only. No broker order execution is enabled.
Build: `pip install -r requirements.txt`
Start: `python main.py`
Render health check: `/ping`
Keep existing Upstox, Telegram and CRON environment variables. Configure `DATABASE_URL` in Render (Environment) with your PostgreSQL connection string (see "PostgreSQL database setup"). Do not paste credentials into GitHub or chat. Remove obsolete `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`, `REQUIRE_PERSISTENT_DB` and `DB_PATH` variables. Optional Normal Trend tuning variables: `NORMAL_ADX_MIN`, `NORMAL_ADX_ESTABLISHED`, `NORMAL_DI_MIN_GAP`, `NORMAL_RSI_LONG_MIN`, `NORMAL_RSI_SHORT_MAX`, `NORMAL_CANDLE_BODY_MIN`, `NORMAL_CLOSE_POS_MIN`, `NORMAL_PULLBACK_ATR`, `NORMAL_MAX_EXTENSION_ATR`, `NORMAL_BREAKOUT_LOOKBACK`. Optional V7 compatibility tuning variables: `V7_MIN_BARS`, `V7_ADX_MIN`, `V7_CROSSOVER_MAX_BARS`, `V7_VWAP_MAX_ATR`, `V7_CHANNEL_MAX_ATR`, `V7_RSI_CALL_MAX`, `V7_RSI_PUT_MIN`, `V7_PULLBACK_TREND_BARS`, `V7_PULLBACK_TOUCH_ATR`.
Verification boundary
Automated tests verify formulas, warm-up indices, Step Channel update order, missing-candle handling and scanner integration. Exact TradingView parity still requires comparing the bot and chart on identical OHLCV candles for the same NSE symbol/session and the same Pine settings. No profitability or live provider-delivery claim is implied by unit tests.

PostgreSQL database setup (required)
In Render choose New → PostgreSQL (same region as the web service). `render.yaml` also declares `nse-fno-db` and wires `DATABASE_URL` automatically when you deploy it as a Blueprint.
For a manually created service, copy the database's Internal Database URL and add it as `DATABASE_URL` in the web service's Environment tab. (Use the External URL only when connecting from outside Render.)
Save and redeploy.
The runtime uses `psycopg` 3. The schema is created automatically at startup, and `postgres://` URLs are accepted. If `DATABASE_URL` is missing or the database cannot be reached, startup fails clearly rather than silently writing to an ephemeral local file. The database stores signals, duplicate-prevention state, Telegram outbox records, IV/OI history and archived candles. Note: Render's free Postgres tier expires after a limited period; use a paid instance for permanent storage.
V7.5 — Database connection recovery
A dropped PostgreSQL connection (psycopg `OperationalError`/`InterfaceError`, e.g. idle timeout or restart) triggers a fresh connection and one retry; ordinary SQL errors are rolled back and raised.
The same bounded recovery applies to batched candle archival inserts.
Generic 404s, authentication errors and unrelated database failures are not retried.
Render's `BOT_VERSION` default is `nse-fno-v7.5`; if an existing Render environment variable overrides it, update that variable after merging.
Signal-only mode remains mandatory: `signal_only=true`, `orders_enabled=false`; no broker order placement is implemented.

NSE exchange ban-list handling
Automatic NSE/NSE Clearing ban-list downloads, dated-cache validation and stock-signal pausing are intentionally disabled. Exchange ban-list membership does not block scanner candidates or alerts. The bot emits a startup warning so operators independently verify the current exchange list before acting. Explicit `MANUAL_BAN` entries remain active as operator-configured exclusions. This does not bypass instrument-master eligibility, fresh quote/option-chain checks, entry validation, stop-loss or risk controls.
