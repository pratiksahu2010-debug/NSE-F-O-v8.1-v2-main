# V3.0 implementation and validation report

## Implemented in this archive

- V3 configuration route: `STRATEGY_MODE=V3` maps to the independent three-engine scanner and `V3_PROFILE_ENABLED=true` selects the new NORMAL/risk profile. SIMPLE remains available for rollback.
- NORMAL universe reduced to 50 fixed symbols; supported indices remain handled separately.
- NORMAL V3 thresholds and trigger changes: ADX 25, RSI directional threshold 50, strict DI direction, RVOL 1.2 when available, 1.5 ATR extension guard, no mandatory MACD dependency, and no mandatory high-body candle gate.
- ORB thresholds: 0.65 ATR breakout clearance, 55% body and 75% close-position; no mandatory retest.
- MEX thresholds: ADX 18, 0.65 ATR impulse, 1.8x range expansion and extension control; live partial-bar MEX also checks ADX.
- Risk output includes TP1 at 1.5R, TP2 at 2.5R and TP3 at 4R; the V3 tracker advances through all three milestones.
- Telegram displays TP3; Render blueprint selects V3, 7.0/10 minimum score, 0.45–0.65 delta range, 1.5R T1 minimum and PAPER_MODE=true.
- Added V3 design and rollout documentation.

## Local checks (second-pass audit)

- `python -m compileall -q .`: passed.
- Full `python -m pytest -q --disable-warnings`: **393 passed, 8 subtests passed**.
- End-to-end scanner tests: 11 passed; fake-provider fixtures no longer waste time retrying static candle snapshots.
- Render YAML parsing and universe-size assertions: passed (NORMAL 50, ORB 100, MEX 100).
- No order-placement API or execution path is present in the Python source; `/status` reports `orders_enabled=false`.
- ZIP integrity (`unzip -t`): passed.

## Not verified / remaining acceptance work

- GitHub Actions was not run: this archive was prepared locally and no commit/push was performed.
- No Render deployment or live broker/Telegram session was performed. Runtime feed health, instrument subscriptions, option-chain freshness and PostgreSQL connectivity must be verified in the user's deployment.
- Existing engine routing still resolves simultaneous same-underlying triggers through one shared priority selection (ORB, MEX, NORMAL). Per-engine independent signal publication for simultaneous same-bar setups is not yet implemented.
- V3 TP3 is now tracked as a final modeled exit after sequential T1 and T2 milestones; this is a signal tracker, not a partial-quantity execution simulator.
- The available replay path does not have historical option-chain snapshots and executable option bid/ask data, so it cannot validate option-premium P&L, IV/theta, or actual fill slippage.
- No accuracy, profitability, win-rate or stop-hit improvement is claimed.

## Second-pass audit fixes

- V3 Normal closed-bar context now rejects neutral/unknown 15m context as well as opposing context in Stage 1 (before option-chain requests) and Stage 3 (final defense); legacy mode retains its previous neutral allowance.
- V3 Normal intrabar triggers now apply the same completed-15m directional context requirement before they can be dispatched.
- Nearby repeated swing obstacles are now checked for V3 NORMAL, MEX and ORB as well as SIMPLE; the first target is capped before the obstacle only when the remaining structural reward still meets the minimum R:R, otherwise the setup is rejected.
- V3 tracker now advances through T1 (move stop to cost), T2 (lock stop at T1), and T3 (final modeled exit). Legacy tracking retains its previous T2 final-exit behavior. This is one-lot signal tracking, not a partial-quantity execution simulator.
- V3 uses the requested 7.0/10 minimum score floor; legacy SIMPLE/LEGACY retains its 8.0 floor. Individual strategy detectors retain their own quality gates and scores.
- ORB high, low, midpoint and width are now available in closed-bar and live-trigger metrics.
- End-to-end fixture tests disable pointless closed-candle readiness retries against static fake-provider snapshots, reducing suite time without weakening the separate readiness tests.
- Added regression coverage for neutral 15m rejection in V3 and the sequential T1/T2/T3 tracker lifecycle.
