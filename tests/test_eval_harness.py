"""Phase 6: eval harness runs green on the frozen benchmark."""
from src.engine.eval_harness import run_benchmark


def test_benchmark_passes():
    report = run_benchmark()
    s = report["summary"]
    assert s["cases"] >= 3
    assert s["routing_acc"] == 1.0
    assert s["mean_recall"] >= 0.8
    assert s["scorer_calibrated"] is True
    assert s["audit_catches"] is True
