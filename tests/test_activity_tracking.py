from datetime import date

from models import Signal
from tests.db_test_utils import make_test_db


def test_daily_activity_separates_signals_deliveries_outcomes_and_rejections():
    db = make_test_db()
    day = date.today().isoformat()
    signal = Signal(
        id="NIFTY-20261105T100000-C", ts=f"{day}T10:05:00+05:30",
        underlying="NIFTY", side="CALL", strategy="SIMPLE_TREND",
    )
    db.save_signal(signal)
    db.enqueue_telegram("alert:test", signal.id, "chat-1", "signal alert")
    db.mark_telegram_delivered("alert:test:chat-1")
    db.add_update(signal.id, "T1", 120.0, "target 1 reached")
    db.add_update(signal.id, "SL", 90.0, "stop loss hit")
    db.record_rejection(
        "reject-event-1", f"{day}T10:10:00+05:30", day, "RELIANCE",
        "PUT", "stage2/3", "risk levels rejected", "structural stop too wide",
    )
    # A repeated scan event with the same stable key must not inflate the count.
    db.record_rejection(
        "reject-event-1", f"{day}T10:10:00+05:30", day, "RELIANCE",
        "PUT", "stage2/3", "risk levels rejected", "structural stop too wide",
    )

    summary = db.activity_summary(day)
    assert summary["signals_generated"] == 1
    assert summary["signals_by_strategy"] == {"SIMPLE_TREND": 1}
    assert summary["signals_by_symbol"] == {"NIFTY": 1}
    assert summary["telegram_deliveries"] == {"delivered": 1, "pending": 0, "failed_attempts": 0}
    assert summary["outcomes"]["target1_hits"] == 1
    assert summary["outcomes"]["stop_loss_hits"] == 1
    assert summary["rejected_setups"]["total"] == 1
    assert summary["rejected_setups"]["by_reason"] == {"risk levels rejected": 1}
    assert summary["rejected_setups"]["by_symbol"] == {"RELIANCE": 1}
    db.close()
