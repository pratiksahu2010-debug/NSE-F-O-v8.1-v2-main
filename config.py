import os


def _env(name, default="", cast=str):
    v = os.getenv(name)
    if v is None or v == "":
        return default
    try:
        return cast(v)
    except Exception:
        return default


def _bool(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on")


class Config:
    # ---- credentials ----
    # Upstox: the 1-year read-only Analytics Token is all a signal-only bot needs (no daily login).
    UPSTOX_ANALYTICS_TOKEN = _env("UPSTOX_ANALYTICS_TOKEN")
    UPSTOX_ANALYTICS_EXPIRES = _env("UPSTOX_ANALYTICS_EXPIRES")   # optional YYYY-MM-DD if the token is not a decodable JWT
    UPSTOX_ACCESS_TOKEN = _env("UPSTOX_ACCESS_TOKEN")             # legacy daily token (manual)
    UPSTOX_TOKEN_FILE = _env("UPSTOX_TOKEN_FILE")
    # Optional daily flow (only used when no valid analytics token): app API key/secret -> approve on phone -> webhook
    UPSTOX_API_KEY = _env("UPSTOX_API_KEY")
    UPSTOX_API_SECRET = _env("UPSTOX_API_SECRET")
    # Dhan: with PIN + TOTP secret the bot generates a fresh 24h token by itself.
    DHAN_CLIENT_ID = _env("DHAN_CLIENT_ID")
    DHAN_ACCESS_TOKEN = _env("DHAN_ACCESS_TOKEN")
    DHAN_PIN = _env("DHAN_PIN")
    DHAN_TOTP_SECRET = _env("DHAN_TOTP_SECRET")
    TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_IDS = [c.strip() for c in _env("TELEGRAM_CHAT_IDS").split(",") if c.strip()]

    # ---- infra ----
    DATABASE_URL = _env("DATABASE_URL")              # PostgreSQL connection string (Render: Internal Database URL)
    DB_CONNECT_TIMEOUT = _env("DB_CONNECT_TIMEOUT", 10, int)            # seconds
    DB_STATEMENT_TIMEOUT_MS = _env("DB_STATEMENT_TIMEOUT_MS", 30000, int)  # server-side per-statement cap
    CRON_SECRET = _env("CRON_SECRET")              # protects /cron/* and /status
    WEBHOOK_SECRET = _env("WEBHOOK_SECRET")        # path secret for /webhook/upstox-token/<secret> (falls back to CRON_SECRET)
    RUN_LOOP = _env("RUN_LOOP", True, _bool)       # false = purely cron-driven (/cron/tick does the work)
    SCAN_WORKERS = _env("SCAN_WORKERS", 6, int)    # parallel provider calls per scan
    MANUAL_BAN = [x.strip().upper() for x in _env("MANUAL_BAN").split(",") if x.strip()]
    DATA_DIR = _env("DATA_DIR", "./cache")
    PORT = _env("PORT", 10000, int)
    LOG_LEVEL = _env("LOG_LEVEL", "INFO")
    PAPER_MODE = _env("PAPER_MODE", True, _bool)   # signals are always alert-only; flag labels the alerts

    # ---- rate limits (calls/sec). Check current provider docs and tune. ----
    UPSTOX_RPS = _env("UPSTOX_RPS", 5.0, float)  # 300/min ceiling; below Upstox standard-API 500/min quota
    UPSTOX_429_RETRIES = _env("UPSTOX_429_RETRIES", 2, int)
    UPSTOX_429_COOLDOWN_SEC = _env("UPSTOX_429_COOLDOWN_SEC", 15.0, float)
    DHAN_DATA_RPS = _env("DHAN_DATA_RPS", 4.0, float)
    DHAN_CHAIN_INTERVAL_SEC = _env("DHAN_CHAIN_INTERVAL_SEC", 3.2, float)  # Dhan option chain: ~1 req / 3s

    # ---- failover ----
    FAIL_THRESHOLD = 3            # consecutive failures before switching provider
    FAILBACK_PROBE_SEC = 60
    FAILBACK_OK_COUNT = 2
    WS_STALE_SEC = _env("WS_STALE_SEC", 12, int)  # no websocket message for this long => stale during market hours

    # ---- universe ----
    # Indices are discovered from the NSE_FO master (every index that has futures/options).
    # This list is only a fallback label set and is not used to filter.
    INDEX_NAMES = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"]

    # ---- scan ----
    SCAN_MINUTES = 5
    SCAN_DELAY_SEC = 8            # wait after candle close so the candle is final (kept: shorter risks reading a not-yet-final candle)
    STAGE1_SCAN_TIMEOUT_SEC = _env("STAGE1_SCAN_TIMEOUT_SEC", 120.0, float)
    STAGE2_PREFETCH_TIMEOUT_SEC = _env("STAGE2_PREFETCH_TIMEOUT_SEC", 45.0, float)
    VIX_FETCH_TIMEOUT_SEC = _env("VIX_FETCH_TIMEOUT_SEC", 12.0, float)
    MAX_CANDLE_LAG_SEC = _env("MAX_CANDLE_LAG_SEC", 180, int)  # reject stale feeds instead of signaling on old bars
    FIRST_SIGNAL_TIME = (9, 30)   # VWAP needs MIN_SESSION_BARS closed candles to mean anything
    LAST_SIGNAL_TIME = (14, 45)
    EXIT_TIME = (15, 15)
    STAGE1_KEEP = 20
    HIST_DAYS = 20                # trading days of indicator history

    # ---- rebuilt balanced signal engine ----
    # Keep disabled until historical replay and paper verification are complete.
    V2_ENGINE_ENABLED = _env("V2_ENGINE_ENABLED", False, _bool)
    V2_MIN_SCORE = _env("V2_MIN_SCORE", 65.0, float)
    V2_MIN_ADX = 22.0  # hard minimum; not lowered by environment overrides
    V2_MIN_RR = _env("V2_MIN_RR", 1.5, float)
    V2_MIN_HISTORY_BARS = _env("V2_MIN_HISTORY_BARS", 220, int)

    # ---- V3 redesign profile ----
    # STRATEGY_MODE=V3 activates the NORMAL/ORB/MEX stack with this threshold
    # profile. Kept explicit for safe rollback and legacy-mode comparison.
    _STRATEGY_MODE_ENV = _env("STRATEGY_MODE", "SIMPLE").strip().upper()
    V3_PROFILE_ENABLED = _env("V3_PROFILE_ENABLED", _STRATEGY_MODE_ENV == "V3", _bool)

    # ---- strategy mode ----
    # SIMPLE (default): one closed-bar rule on EMA 9/21 + session VWAP + RSI(14) + ADX(14) >= SIMPLE_ADX_MIN
    # (strategies/simple_signal.py). LEGACY: the previous multi-engine stack (Normal Trend / MEX / ORB / live
    # intrabar); kept only so old and new behaviour can be compared. Anything other than LEGACY means SIMPLE.
    STRATEGY_MODE = "LEGACY" if _STRATEGY_MODE_ENV in {"LEGACY", "V3"} else "SIMPLE"
    SIMPLE_ADX_MIN = _env("SIMPLE_ADX_MIN", 25.0, float)               # trend must be real: ADX(14) at or above this
    SIMPLE_RSI_CALL_MIN = _env("SIMPLE_RSI_CALL_MIN", 52.0, float)     # CALL: RSI at or above ...
    SIMPLE_RSI_CALL_MAX = _env("SIMPLE_RSI_CALL_MAX", 68.0, float)     # ... but not an overbought spike
    SIMPLE_RSI_PUT_MAX = _env("SIMPLE_RSI_PUT_MAX", 48.0, float)       # PUT: RSI at or below ...
    SIMPLE_RSI_PUT_MIN = _env("SIMPLE_RSI_PUT_MIN", 32.0, float)       # ... but not an oversold flush
    SIMPLE_FRESH_BARS = _env("SIMPLE_FRESH_BARS", 3, int)              # fire only while the setup is at most this many candles old
    SIMPLE_MAX_EXTENSION_ATR = _env("SIMPLE_MAX_EXTENSION_ATR", 1.5, float)  # reject chasing >1.5 ATR from EMA9
    SIMPLE_MIN_BARS = _env("SIMPLE_MIN_BARS", 35, int)                 # candles of history before the indicators are trusted (ADX needs 29 + the freshness window)
    SIMPLE_MIN_SESSION_BARS = _env("SIMPLE_MIN_SESSION_BARS", 3, int)  # closed candles today before VWAP is trusted (first scan 09:30)
    SIMPLE_MIN_RR_T1 = _env("SIMPLE_MIN_RR_T1", 1.5, float)  # Normal/SIMPLE first target must retain at least 1.5R after structure
    # Underlying ticks feed a separate partial-candle detector. SIMPLE uses the same EMA/VWAP/RSI/ADX rules
    # on the live partial bar; LEGACY retains its independent intrabar Normal/MEX/ORB detectors.
    LIVE_INTRABAR_ENABLED = _env("LIVE_INTRABAR_ENABLED", True, _bool)
    # V8.1 - the scan runs SCAN_DELAY_SEC after the candle boundary. If the provider has not published the just-closed
    # candle yet, the symbol used to be discarded as "stale" and that candle was never evaluated. Now the scanner waits
    # briefly for it (shared across all workers, so a slow feed costs one short wait per scan, not one per symbol).
    CANDLE_READY_RETRIES = _env("CANDLE_READY_RETRIES", 4, int)           # extra fetches while the closed candle is missing (0 = off)
    CANDLE_READY_WAIT_SEC = _env("CANDLE_READY_WAIT_SEC", 3.0, float)     # pause between those fetches
    CANDLE_READY_MAX_WAIT_SEC = _env("CANDLE_READY_MAX_WAIT_SEC", 45.0, float)  # never wait past this long after the candle closed
    # If 5m history was missing at bootstrap (429 / token / network) the indicators need ~35 candles of today alone
    # (about 12:00). Re-fetch history for short symbols during the scan instead of staying blind all morning.
    HISTORY_BACKFILL_ENABLED = _env("HISTORY_BACKFILL_ENABLED", True, _bool)
    HISTORY_BACKFILL_PER_SCAN = _env("HISTORY_BACKFILL_PER_SCAN", 25, int)       # cap of history requests per scan
    HISTORY_BACKFILL_MAX_ATTEMPTS = _env("HISTORY_BACKFILL_MAX_ATTEMPTS", 3, int)  # per symbol per day
    # Telegram warning when this share of the universe has no usable candles (failed / timed out / stale / empty).
    DATA_HEALTH_ALERT_FRAC = _env("DATA_HEALTH_ALERT_FRAC", 0.5, float)
    DATA_HEALTH_ALERT_COOLDOWN_SEC = _env("DATA_HEALTH_ALERT_COOLDOWN_SEC", 1800, int)

    # ---- scoring ----
    # Live alerts use one numerical score gate only: ALERT_SCORE_MIN on the 0-10 indicator score.
    # OI/option-chain direction and 15-minute alignment are not approval gates. The option chain remains
    # necessary to select a valid contract and validate its executable quote.
    REGIME_BONUS = 5.0            # legacy scorer context only
    # V3 uses the specification's 7.0/10 standard-alert floor; older SIMPLE/
    # LEGACY modes retain the established 8.0 floor. RESEARCH_MODE is for
    # offline replay only and must never be enabled on the live alert service.
    RESEARCH_MODE = _env("RESEARCH_MODE", False, _bool)
    ALERT_SCORE_FLOOR = 0.0 if RESEARCH_MODE else (7.0 if V3_PROFILE_ENABLED else 8.0)
    ALERT_SCORE_MIN = max(ALERT_SCORE_FLOOR, _env("ALERT_SCORE_MIN", ALERT_SCORE_FLOOR, float))
    if ALERT_SCORE_MIN > 10.0:
        raise ValueError("ALERT_SCORE_MIN must not exceed 10")
    PRICE_MIN_SCORE = 72.0  # legacy price-strategy compatibility value; not a live candidate/alert gate
    CANDIDATE_MIN_PRICE_SCORE = 0.0  # active compatibility threshold; real setup votes are still required
    MIN_CONFIDENCE = 80.0  # LEGACY: only used by scorer.evaluate()/combine(), which the live scanner no longer calls
    CHOP_PENALTY = 0.92
    EXPIRY_DAY_PENALTY = 0.95

    # ---- price strategy: EMA 9/21 + session VWAP + RSI(14) + ADX(14) on 5m candles ----
    MIN_BARS = 30                 # candles of history needed (ADX needs 2*14+1 = 29)
    MIN_SESSION_BARS = 3          # closed candles today before VWAP is trusted (first signal ~09:30)
    # Candidate discovery is deliberately separate from the 10-point live-alert gate.
    # Candidates need a real setup vote; Telegram requires score >= ALERT_SCORE_MIN (8/10) plus every hard gate.
    MAX_OPTION_CHAIN_AGE_SEC = _env("MAX_OPTION_CHAIN_AGE_SEC", 60, int)
    # Intrabar alerts use a stricter chain-age ceiling. The REST request is
    # refreshed asynchronously; a cache entry alone never bypasses quote-age checks.
    LIVE_CHAIN_CACHE_TTL_SEC = _env("LIVE_CHAIN_CACHE_TTL_SEC", 4.0, float)
    LIVE_CHAIN_RETRY_SEC = _env("LIVE_CHAIN_RETRY_SEC", 2.0, float)
    LIVE_CHAIN_MAX_AGE_SEC = _env("LIVE_CHAIN_MAX_AGE_SEC", 8.0, float)
    # Option-chain quote validation (independent of response age): chain spot vs the underlying's last candle,
    # and quote sanity on the ATM window the signal depends on.
    CHAIN_SPOT_TOL_PCT = _env("CHAIN_SPOT_TOL_PCT", 0.5, float)
    # The picker only considers ATM +/-2 strikes and checks bid/ask on the one it chooses, so this check exists to catch a
    # broken snapshot, not to demand two-sided quotes on far strikes (one-sided far strikes are normal for stock options).
    CHAIN_ATM_STRIKES = _env("CHAIN_ATM_STRIKES", 2, int)            # strikes each side of ATM to validate
    CHAIN_MAX_MISSING_QUOTE_FRAC = _env("CHAIN_MAX_MISSING_QUOTE_FRAC", 0.4, float)
    STAGE1_MIN_SCORE = 0.0  # no separate candidate score threshold
    EARLY_ENABLED = False  # safety switch: provisional previews are not published as alerts
    # Signal modules are enabled by default but remain independently configurable. Every alert still passes the shared confirmation, score, chop, timing, risk and duplicate gates.
    # Counter-trend reversal has stricter stretch/RSI/structure requirements inside score_reversal().
    ORB_ENABLED = _env("ORB_ENABLED", True, _bool)
    VWAP_SETUP_ENABLED = _env("VWAP_SETUP_ENABLED", True, _bool)
    SQUEEZE_ENABLED = _env("SQUEEZE_ENABLED", True, _bool)
    MOMENTUM_EXPLOSION_ENABLED = _env("MOMENTUM_EXPLOSION_ENABLED", True, _bool)
    # Dedicated impulse detector thresholds (5-minute underlying candles).
    MEX_ENABLED = _env("MEX_ENABLED", True, _bool)
    MEX_COMPRESSION_MAX = _env("MEX_COMPRESSION_MAX", 0.90, float)
    MEX_LOOKBACK = _env("MEX_LOOKBACK", 8, int)
    MEX_MIN_RANGE_RATIO = _env("MEX_MIN_RANGE_RATIO", 1.8, float)
    MEX_MIN_VOL_RATIO = _env("MEX_MIN_VOL_RATIO", 1.5, float)
    MEX_MIN_IMPULSE_ATR = _env("MEX_MIN_IMPULSE_ATR", 0.65, float)
    MEX_MIN_ACCEL_ATR = _env("MEX_MIN_ACCEL_ATR", 0.25, float)
    MEX_CLOSE_POS = _env("MEX_CLOSE_POS", 0.75, float)
    MEX_MAX_EXTENSION_ATR = _env("MEX_MAX_EXTENSION_ATR", 1.8, float)
    # Independent Momentum Explosion + Opening Range Breakout engine (5m completed candles).
    MOMENTUM_ORB_ENABLED = _env("MOMENTUM_ORB_ENABLED", True, _bool)
    MEX_ORB_LAST_TIME = (_env("ORB_LAST_HOUR", 14, int), _env("ORB_LAST_MINUTE", 45, int))
    MEX_ORB_CLEARANCE_ATR = _env("MEX_ORB_CLEARANCE_ATR", 0.65, float)
    MEX_ORB_BODY_MIN = _env("MEX_ORB_BODY_MIN", 0.55, float)
    MEX_ORB_CLOSE_POS = _env("MEX_ORB_CLOSE_POS", 0.75, float)
    MEX_ORB_MIN_RANGE_RATIO = _env("MEX_ORB_MIN_RANGE_RATIO", 1.0, float)
    MEX_ORB_COMPRESSION_MAX = _env("MEX_ORB_COMPRESSION_MAX", 0.95, float)
    MEX_ORB_MAX_EXTENSION_ATR = _env("MEX_ORB_MAX_EXTENSION_ATR", 1.5, float)
    MEX_ORB_RETEST_ATR = _env("MEX_ORB_RETEST_ATR", 0.20, float)
    MEX_ORB_RETEST_REQUIRED = False  # retest is preferred context, never a compulsory second-candle gate
    MEX_ORB_SCORE_MIN = _env("MEX_ORB_SCORE_MIN", 70.0, float)
    # Per-strategy floors are disabled: all strategies use ALERT_SCORE_MIN only.
    STRATEGY_SCORE_MIN = ""
    ORB_BARS = 3                  # opening range = first 3 five-minute candles (09:15-09:30)
    ORB_LAST_TIME = MEX_ORB_LAST_TIME  # fresh ORB breaks allowed through the normal signal cutoff
    ORB_MIN_RANGE_ATR, ORB_MAX_RANGE_ATR = 0.6, 3.5
    VWAP_WRONG_BARS = 3           # candles (of the previous 6) closed on the wrong side of VWAP before the reclaim
    VWAP_MIN_CROSS_ATR = 0.10     # close must clear VWAP by this many ATR
    SQ_BOX_BARS = 8               # squeeze box = previous 8 candles
    SQ_ATR_RATIO = 0.75           # ATR before the breakout <= 75% of its 30-candle peak
    SQ_BOX_ATR = 2.6              # box height <= 2.6 x ATR
    SQ_EXPANSION = 1.3            # breakout candle range >= 1.3 x average box candle
    # Accuracy helpers (quality, not just stricter): penalise chasing and rejection wicks, require confluence agreement
    CHASE_ATR = 1.2               # close further than this from EMA9 (in ATR) starts costing points
    CONFLUENCE_BONUS = 4.0        # extra points per additional setup type that agrees on the same side (max 2)
    CONFLICT_MARGIN = 10.0        # opposite-side setup within this many points of the best one -> skip as ambiguous
    ADAPT_ENABLED = _env("ADAPT_ENABLED", True, _bool)   # raise/lower the bar per signal type from its own tracked results
    ADAPT_DAYS = _env("ADAPT_DAYS", 45, int)
    ADAPT_MIN_N = _env("ADAPT_MIN_N", 40, int)           # 12 trades is noise; need a real sample before the bar moves
    ADAPT_TOD_MINUTES = 30                                # time-of-day buckets learned from own outcomes
    ADAPT_TOD_CAP_PTS = 3.0                               # a time bucket can raise the bar by at most this (0-100 scale)
    REV_MIN_VOL = 0.9             # reversal candle volume must be at least this x the 20-candle average (when volume exists)
    REVERSAL_ENABLED = _env("REVERSAL_ENABLED", True, _bool)
    SETUP_MIN_ADX = _env("SETUP_MIN_ADX", 20.0, float)       # balanced floor for breakout/VWAP/squeeze/impulse triggers
    # Normal Trend: shared closed-bar and intrabar detector thresholds.
    # ADX 20-25 is allowed only while ADX is rising; >=25 is established trend.
    NORMAL_ADX_MIN = _env("NORMAL_ADX_MIN", 20.0, float)
    NORMAL_ADX_ESTABLISHED = _env("NORMAL_ADX_ESTABLISHED", 25.0, float)
    NORMAL_DI_MIN_GAP = _env("NORMAL_DI_MIN_GAP", 3.0, float)
    NORMAL_RSI_LONG_MIN = _env("NORMAL_RSI_LONG_MIN", 48.0, float)
    NORMAL_RSI_SHORT_MAX = _env("NORMAL_RSI_SHORT_MAX", 52.0, float)
    NORMAL_CANDLE_BODY_MIN = _env("NORMAL_CANDLE_BODY_MIN", 0.35, float)
    NORMAL_CLOSE_POS_MIN = _env("NORMAL_CLOSE_POS_MIN", 0.62, float)
    NORMAL_PULLBACK_ATR = _env("NORMAL_PULLBACK_ATR", 0.20, float)
    NORMAL_MAX_EXTENSION_ATR = _env("NORMAL_MAX_EXTENSION_ATR", 0.60, float)
    NORMAL_BREAKOUT_LOOKBACK = _env("NORMAL_BREAKOUT_LOOKBACK", 6, int)
    REVERSAL_MIN_ADX = _env("REVERSAL_MIN_ADX", 18.0, float)  # balanced counter-trend floor; reversal candle/RSI/extreme gates remain mandatory
    REV_STRETCH_ATR = 2.0         # price must have been this many ATR beyond EMA21 within the last REV_LOOKBACK candles
    REV_LOOKBACK = 8
    REV_RSI_OS, REV_RSI_OB = 32.0, 68.0
    REV_STOP_BARS = 6             # reversal stop sits beyond the low/high of this many candles
    BREAKOUT_BARS = 6             # trigger: close beyond the previous N-candle high / low
    MIN_ATR_PCT = 0.03            # 5m ATR as % of price: below this the premium cannot pay for the move
    VOL_OK, VOL_STRONG = 1.0, 1.5  # last-candle volume / 20-candle average
    EMA_FAST, EMA_SLOW = 9, 21
    ADX_MIN = 25.0                # live-alert hard gate: ADX must be >=25 independently of score
    ADX_RISING_FLOOR = 14.0       # ...or a trend that is just building: ADX above this AND rising by ADX_RISING_DELTA
    ADX_RISING_DELTA = 2.0
    DI_MIN_GAP = 2.0              # +DI must lead -DI (Call) / -DI must lead +DI (Put) by at least this much
    EMA_MIN_SEP_ATR = 0.10        # |EMA9-EMA21| in ATR: flat / tangled EMAs are not a trend
    EMA_MAX_CROSSES = 2           # EMA9/21 flip-flops in the last 20 candles: more than this = chop
    CHOP_GATE_ENABLED = _env("CHOP_GATE_ENABLED", True, _bool)
    CHOP_BREAKOUT_RANGE_RATIO = _env("CHOP_BREAKOUT_RANGE_RATIO", 1.3, float)
    CHOP_BREAKOUT_VOL_RATIO = _env("CHOP_BREAKOUT_VOL_RATIO", 1.2, float)
    CROSS_FRESH_BARS = 4          # EMA9/21 cross trigger must be at most this many candles old (0 = latest candle)
    PB_MIN_TREND_BARS = 6         # pullback trigger: EMA9 must have stayed on the trend side of EMA21 this long
    PB_TOUCH_ATR = 0.15           # pullback must come within this many ATR of EMA9
    RSI_BULL_MIN = 50.0           # Call needs RSI above this and rising ...
    RSI_CALL_MAX = 75.0           # ... but never a Call into an overbought spike
    RSI_BEAR_MAX = 50.0           # Put needs RSI below this and falling ...
    RSI_PUT_MIN = 25.0            # ... but never a Put into an oversold flush
    MAX_EXTENSION_ATR = 2.0       # normal trend: max directional extension from EMA9 (in 5m ATR)
    CLOSE_POS_MIN = 0.65          # trigger candle must close in the top (Call) / bottom (Put) 35% of its range
    VWAP_MAX_CROSSES = 2          # price crossing VWAP more than this in 12 candles = chop
    VWAP_MAX_DIST_ATR = 2.0       # close further than this from VWAP = stretched
    HTF_MINUTES = 15
    HTF_OPPOSED_PENALTY = 0.88
    HTF_NEUTRAL_PENALTY = 0.97    # 15m trend mixed
    MKT_OPPOSED_PENALTY = 0.92    # stock signal against the NIFTY 15m trend
    OI_REF_MAX_AGE_MIN = 90       # OI snapshot older than this is not a valid "30 minutes ago" reference
    PCR_MIN_CHANGE = 0.08         # whole-chain PCR must move at least this to count
    # Spot is read from the option chain 30-90 s after the candle closes (REST scan), so the drift limits must leave room for that.
    LIVE_MAX_ADVERSE_ATR = _env("LIVE_MAX_ADVERSE_ATR", 0.50, float)   # spot already reversed this far since the signal candle closed = skip
    LIVE_MAX_RUNAWAY_ATR = _env("LIVE_MAX_RUNAWAY_ATR", 0.80, float)   # spot already ran this far = entry is a chase
    INDEX_VWAP_FROM_FUTURES = _env("INDEX_VWAP_FROM_FUTURES", True, _bool)   # index VWAP weighted by current-month futures volume
    MIDDAY_WINDOW = ((11, 45), (13, 30))
    MIDDAY_EXTRA_CONF = 3.0       # lunch-hour chop: demand more confidence
    LATE_AFTER = (14, 15)
    LATE_EXTRA_CONF = 3.0         # little time left for the trade to work
    GAP_DECAY_MIN = 195           # gap bias fades to zero by ~12:30

    # ---- context gates (accuracy v6). Each can be switched off; judge them with tools/replay.py ----
    HTF_VETO_ENABLED = _env("HTF_VETO_ENABLED", True, _bool)      # 15m trend opposed -> no trend/ORB/VWAP/squeeze/MEX signal
    MKT_VETO_ENABLED = _env("MKT_VETO_ENABLED", True, _bool)      # stock signal against NIFTY 15m trend (not for reversals)
    RS_GATE_ENABLED = _env("RS_GATE_ENABLED", True, _bool)        # stock must not lag (CALL) / lead (PUT) NIFTY on the day
    RS_TOLERANCE_PCT = _env("RS_TOLERANCE_PCT", 0.15, float)
    WALL_GATE_ENABLED = _env("WALL_GATE_ENABLED", True, _bool)    # entering right under a dominant call wall / above a put wall
    WALL_MIN_ATR = _env("WALL_MIN_ATR", 1.0, float)               # wall closer than this many 5m ATR = target room is blocked
    WALL_DOMINANCE = _env("WALL_DOMINANCE", 2.0, float)           # wall OI >= this x mean OI of that side's strikes
    VOL_TOD_GATE_ENABLED = _env("VOL_TOD_GATE_ENABLED", True, _bool)  # trigger candle volume vs SAME time-of-day on prior days
    VOL_TOD_MIN = _env("VOL_TOD_MIN", 0.8, float)
    VOL_TOD_MIN_DAYS = 3
    FUT_OI_BARS = 3

    # ---- outcome realism (tracker / adaptive learning) ----
    REALISTIC_EXITS = _env("REALISTIC_EXITS", True, _bool)   # exits filled on the bid side (half-spread slippage) and net of costs
    EXIT_SLIP_MIN_PCT = _env("EXIT_SLIP_MIN_PCT", 0.5, float)
    EXIT_SLIP_MAX_PCT = _env("EXIT_SLIP_MAX_PCT", 3.0, float)
    # Cost model for ONE round trip of an option buy. Verify against your broker contract note and current NSE/SEBI
    # rates; rates change. Percentages are of premium turnover.
    BROKERAGE_PER_ORDER = _env("BROKERAGE_PER_ORDER", 20.0, float)
    STT_SELL_PCT = _env("STT_SELL_PCT", 0.10, float)
    EXCH_TXN_PCT = _env("EXCH_TXN_PCT", 0.0350, float)
    SEBI_PCT = _env("SEBI_PCT", 0.0001, float)
    STAMP_BUY_PCT = _env("STAMP_BUY_PCT", 0.003, float)
    GST_PCT = _env("GST_PCT", 18.0, float)

    # ---- data archive for replay / calibration ----
    CANDLE_ARCHIVE_ENABLED = _env("CANDLE_ARCHIVE_ENABLED", True, _bool)   # closed 5m session candles saved to the DB
    CANDLE_KEEP_DAYS = _env("CANDLE_KEEP_DAYS", 120, int)

    # ---- contract picker ----
    INDEX_MIN_DTE = 1
    STOCK_MIN_DTE = 2
    MAX_SPREAD_PCT = _env("MAX_SPREAD_PCT", 3.0, float)              # INDEX options
    MAX_SPREAD_PCT_STOCK = _env("MAX_SPREAD_PCT_STOCK", 4.0, float)  # STOCK options
    MIN_OI_LOTS = {"INDEX": 100, "STOCK": 20}       # OI / lot_size. Upstox/Dhan report OI in units.
    MIN_VOLUME_LOTS = {"INDEX": 20, "STOCK": 3}
    MIN_OPTION_DELTA = _env("MIN_OPTION_DELTA", 0.40, float)
    MAX_OPTION_DELTA = _env("MAX_OPTION_DELTA", 0.70, float)
    TARGET_DELTA = 0.55
    MAX_IV_RANK = 90.0
    MIN_PREMIUM = 5.0

    # ---- risk ----
    SL_ATR = 1.0                  # default underlying stop distance (in 5m ATR) when no structural stop is supplied
    T1_ATR = 1.5
    T2_ATR = 2.0
    STOP_BUFFER_ATR = 0.25        # structural stop sits this far beyond the nearer of EMA21 / VWAP (trend invalidation)
    SL_MIN_ATR = 0.7              # ...but never tighter than this (noise) ...
    SL_MAX_ATR = _env("SL_MAX_ATR", 1.3, float)              # ...or wider than this (premium risk)
    T1_R, T2_R, T3_R = 1.5, 2.5, 4.0         # targets are never closer than this many multiples of the stop distance
    MAX_SL_PCT = 0.30             # premium stop never wider than 30%
    MIN_RR_T1 = _env("MIN_RR_T1", 1.5 if V3_PROFILE_ENABLED else 1.2, float)
    ENTRY_BAND_DOWN = 0.002
    ENTRY_BAND_UP = 0.004
    VALID_MINUTES = 10
    SCAN_DIGEST_EVERY = _env("SCAN_DIGEST_EVERY", 6, int)   # Telegram 'why no signal' digest every N scans without a signal (0 = off)
    PING_RUNS_TICK = False  # compatibility only; /ping is always keep-alive-only
    TRACK_INTERVAL_SEC = 4
    REST_PREMIUM_REFRESH_SEC = 20


    # Correlation labels remain descriptive metadata only; they do not limit signal publication.
    GROUPS = {
        "BANK": ["BANKNIFTY", "HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK", "INDUSINDBK", "BANDHANBNK",
                 "FEDERALBNK", "IDFCFIRSTB", "PNB", "BANKBARODA", "AUBANK"],
        "IT": ["TCS", "INFY", "WIPRO", "HCLTECH", "TECHM", "LTIM", "PERSISTENT", "COFORGE", "MPHASIS"],
        "AUTO": ["MARUTI", "TATAMOTORS", "M&M", "BAJAJ-AUTO", "EICHERMOT", "HEROMOTOCO", "TVSMOTOR", "ASHOKLEY"],
        "METAL": ["TATASTEEL", "JSWSTEEL", "HINDALCO", "VEDL", "JINDALSTEL", "SAIL", "NMDC"],
        "ENERGY": ["RELIANCE", "ONGC", "BPCL", "IOC", "GAIL", "NTPC", "POWERGRID", "COALINDIA"],
        "FIN": ["FINNIFTY", "BAJFINANCE", "BAJAJFINSV", "HDFCLIFE", "SBILIFE", "CHOLAFIN", "SHRIRAMFIN"],
    }

    UPSTOX_VIX_KEY = "NSE_INDEX|India VIX"
    DHAN_VIX_ID = 21
    EVENTS_FILE = _env("EVENTS_FILE", "./events.json")
    HOLIDAYS_FILE = _env("HOLIDAYS_FILE", "./holidays.json")

    # V7.1 signal-only sequential engine settings; no broker order routing exists.
    V7_SIGNAL_ONLY = True
    V7_ORDERS_ENABLED = False
    V7_PRIMARY_TIMEFRAME = "5m"
    V7_CONTEXT_TIMEFRAME = "15m"
    V7_ADX_MIN = _env("V7_ADX_MIN", 25.0, float)
    V7_MIN_BARS = _env("V7_MIN_BARS", 50, int)
    V7_CROSSOVER_MAX_BARS = _env("V7_CROSSOVER_MAX_BARS", 3, int)
    V7_VWAP_MAX_ATR = _env("V7_VWAP_MAX_ATR", 1.5, float)
    V7_RSI_CALL_MAX = _env("V7_RSI_CALL_MAX", 75.0, float)
    V7_RSI_PUT_MIN = _env("V7_RSI_PUT_MIN", 25.0, float)
    V7_PULLBACK_TREND_BARS = _env("V7_PULLBACK_TREND_BARS", 6, int)
    V7_PULLBACK_TOUCH_ATR = _env("V7_PULLBACK_TOUCH_ATR", 0.15, float)


cfg = Config()
