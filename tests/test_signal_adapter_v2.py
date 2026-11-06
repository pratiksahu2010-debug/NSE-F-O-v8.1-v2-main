from datetime import datetime, timedelta, timezone

from models import Candle as LegacyCandle
from signal_engine_v2.adapter import build_snapshot


def history(n=230):
    start = datetime(2026, 11, 3, 9, 15, tzinfo=timezone.utc)
    out = []
    price = 100.0
    for i in range(n):
        price += .05
        out.append(LegacyCandle(start + timedelta(minutes=5*i), price-.02,
                                price+.1, price-.1, price, 1000, 0))
    return out


def test_adapter_uses_only_closed_bars_for_indicators_when_live_bar_exists():
    bars = history()
    live = LegacyCandle(bars[-1].ts + timedelta(minutes=5), 112, 120, 110, 119, 5000, 0)
    snap = build_snapshot("RELIANCE", "STOCK", "5m", bars,
                          live_candle=live)
    assert snap.candle.is_closed is False
    assert snap.indicators["ema_200"] is not None
    # The partial live close is not appended to the confirmed historical candle set.
    assert all(x.is_closed for x in snap.candles)
    assert snap.candles[-1].close == bars[-1].c


def test_index_volume_proxy_is_not_used_with_incomplete_history_coverage():
    bars = history()
    partial_map = {bars[-1].ts: 2000}
    snap = build_snapshot("NIFTY", "INDEX", "5m", bars,
                          futures_volume_by_timestamp=partial_map)
    assert snap.metadata["volume_source"] == "unavailable"
    assert snap.indicators["rvol_20"] is None


def test_index_volume_proxy_is_used_when_history_is_covered():
    bars = history()
    vol_map = {bar.ts: 2500 + i for i, bar in enumerate(bars)}
    snap = build_snapshot("NIFTY", "INDEX", "5m", bars,
                          futures_volume_by_timestamp=vol_map)
    assert snap.metadata["volume_source"] == "current_month_future_volume"
    assert snap.candles[-1].volume > 0
    assert snap.indicators["rvol_20"] is not None
