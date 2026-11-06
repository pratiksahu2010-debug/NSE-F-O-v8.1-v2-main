from datetime import datetime, timedelta
from tools.replay_v2 import simulate_outcome


def test_stop_wins_when_stop_and_target_are_touched_in_same_candle():
    now = datetime(2026, 11, 5, 10, 0)
    future = [type("Bar", (), {"l": 94, "h": 111, "c": 100,
                               "ts": now + timedelta(minutes=5)})()]
    r, reason, used, _ = simulate_outcome("CALL", 100, 95, 105, 110, future)
    assert r == -1
    assert reason == "SL"
    assert used == 1

