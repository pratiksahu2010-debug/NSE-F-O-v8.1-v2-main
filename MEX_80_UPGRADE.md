# Dedicated Momentum Explosion (MEX) update

- Added a fixed 80-name NSE F&O MEX watchlist (`strategies/mex_universe.py`).
- Bot bootstrap reserves capacity for all eligible MEX names plus discovered indices. Actual membership is filtered by the current Upstox NSE_FO instrument master; unavailable names are not fabricated or traded.
- Added a dedicated Momentum Expansion Index (MEI): relative range expansion, relative volume, candle impulse/acceleration, prior compression, directional close, ADX/DI, EMA9/21, RSI, VWAP and extension controls.
- MEX is evaluated only for the fixed MEX watchlist. An unconfirmed MEX vote is excluded from ordinary strategy scoring; the persistent MEX lifecycle remains the only path to promote a confirmed MEX vote.
- ADX >=25 and directional DI are mandatory in the MEX detector. Existing contract selection, risk validation and signal-only alert path remain downstream.
- MEX compression, thresholds and enablement are configurable with `MEX_ENABLED`, `MEX_COMPRESSION_MAX` and existing `MEX_*` settings.

## Safety / limitations

This is still a signal-only system. The fixed list is a candidate universe, not a guarantee that all 80 symbols are current F&O constituents. Each boot checks the Upstox instrument master. No order placement was added. No performance or accuracy guarantee is claimed; replay and forward paper validation are required before relying on alerts.

## Final integration pass (November 2026)

- Added `strategies/mex_scanner.py`: a dedicated MEX engine owns the fixed 80-symbol eligibility list, detector invocation, persistent lifecycle and per-cycle diagnostics. It consumes the same already-fetched closed 5-minute candles as the main scanner, so it does not duplicate market-data requests.
- Wired the engine into `engine/scanner.py`. Unconfirmed MEX candidates remain excluded from ordinary strategy votes; only the lifecycle confirmation candle can be promoted.
- Replaced active runtime Dhan failover with `data/upstox_only.py`. Upstox errors now surface directly and are recorded in health diagnostics; there is no silent switch to a second broker. The token refresh route and Render environment template are also Upstox-only. Legacy Dhan/failover modules and their isolated unit tests remain in the source tree but are not instantiated by the live bot.
- Added focused tests for the single-provider adapter and dedicated MEX engine.

The V6 sequential engine remains a separate research module and is not yet a mandatory live alert gate. Do not interpret this package as a validated profitability/accuracy guarantee; forward testing and historical out-of-sample validation remain necessary.
