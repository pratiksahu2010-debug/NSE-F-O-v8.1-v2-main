# V7.8 — Fixed NSE F&O universes and dedicated Momentum+ORB

## Symbol groups

- **Normal engine:** all eligible NSE F&O indices discovered from the current Upstox instrument master, plus a fixed 50-stock list in strategies/normal_universe.py.
- **Momentum Explosion:** a separate fixed 100-stock list in strategies/mex_universe.py.
- The normal 50 are deliberately included within the 100-stock MEX list. This keeps the normal set exactly 50 while allowing one shared market-data/history fetch for the 100-stock union.
- No turnover ranking, cache, priority additions, or instrument-master ordering changes static membership. At bootstrap, each list is intersected with the live NSE_FO master. Missing/ineligible names are reported in diagnostics and excluded; no synthetic symbols are created.
- All eligible index instruments are retained. MAX_UNDERLYING_SYMBOLS/TOP_STOCKS are compatibility settings and do not override the fixed lists.

## Dedicated Momentum Explosion + ORB

The MEX 100-stock universe is evaluated by an independent Momentum+ORB engine in strategies/momentum_orb.py. It has its own indicator calculation and lifecycle, separate from the normal ORB and the MEX detector.

The first three contiguous completed 5-minute bars from 09:15 define the opening range. A fresh breakout is only a candidate when the opening-range width, compression, range expansion, relative volume, EMA9/21 direction, session VWAP, RSI, ADX/DI, candle close location/body and extension checks align. The candidate is not alerted immediately: the next completed 5-minute candle must retest/hold the ORB edge and close back in the breakout direction. A failed retest expires or invalidates the candidate.

The dedicated score is 0–100 with a configurable floor (MEX_ORB_SCORE_MIN=70). The resulting signal uses the ORB structural invalidation level, while still passing the normal downstream checks for fresh option chain, contract validity, live price, executable ask and risk levels. Optional context vetoes do not override a fully confirmed Momentum+ORB setup; context remains visible in the signal diagnostics.

## Safety and validation

- Completed 5-minute candles only; existing 180-second stale-candle guard remains unchanged.
- Existing 15-minute context is retained as information, not a veto for the independent Momentum+ORB signal.
- Telegram alerts only after next-candle retest confirmation.
- Signal-only: signal_only=true, orders_enabled=false; no broker order placement.
- No estimated fees/spread/slippage/net-profit rejection gate was added.
- The fixed lists are curated watchlists, not a guarantee of current eligibility or profitability. Upstox instrument-master validation and paper/replay testing remain required.