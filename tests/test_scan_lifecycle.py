import pytest

from engine.scanner import Scanner


def _scanner_with_run(run):
    scanner = object.__new__(Scanner)
    scanner.unds = []
    scanner.last_cycle_stage = "idle"
    scanner.last_cycle_stage_seconds = {}
    scanner._cycle_stage_started = None
    scanner.last_cycle_started_at = None
    scanner.last_cycle_at = None
    scanner.last_cycle_error = None
    scanner.last_cycle_seconds = None
    scanner._run_once = run
    return scanner


def test_run_once_records_successful_cycle_and_duration():
    scanner = _scanner_with_run(lambda now: ["signal"])
    result = scanner.run_once(None)
    assert result == ["signal"]
    assert scanner.last_cycle_started_at
    assert scanner.last_cycle_at
    assert scanner.last_cycle_stage == "completed"
    assert scanner.last_cycle_error is None
    assert scanner.last_cycle_seconds >= 0


def test_run_once_records_failed_stage_and_reraises():
    def fail(_now):
        scanner._set_cycle_stage("stage1_underlying_scan")
        raise RuntimeError("provider failed")

    scanner = _scanner_with_run(fail)
    with pytest.raises(RuntimeError, match="provider failed"):
        scanner.run_once(None)
    assert scanner.last_cycle_started_at
    assert scanner.last_cycle_at is None
    assert scanner.last_cycle_stage == "failed"
    assert scanner.last_cycle_error == "RuntimeError: provider failed"
    assert scanner.last_cycle_seconds >= 0
    assert scanner.last_cycle_stage_seconds["stage1_underlying_scan"] >= 0
