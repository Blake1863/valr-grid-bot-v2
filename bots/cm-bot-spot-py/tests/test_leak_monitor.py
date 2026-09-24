import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.leak_monitor import Classification, LeakMonitor


def test_no_alert_below_min_samples():
    lm = LeakMonitor(external_alert_threshold=0.1, min_samples_for_alert=20)
    for _ in range(5):
        lm.record("BTCZAR", Classification.EXTERNAL)
    assert lm.should_alert("BTCZAR") is None


def test_alert_fires_on_breach():
    lm = LeakMonitor(external_alert_threshold=0.10, min_samples_for_alert=20, alert_cooldown_seconds=60)
    # 25 cycles, 4 external (16%) — above 10% threshold
    for i in range(25):
        c = Classification.EXTERNAL if i < 4 else Classification.INTERNAL
        lm.record("BTCZAR", c)
    s = lm.should_alert("BTCZAR")
    assert s is not None
    assert s["external_ratio"] > 0.10


def test_cooldown_suppresses_repeat():
    lm = LeakMonitor(external_alert_threshold=0.10, min_samples_for_alert=20, alert_cooldown_seconds=60)
    for i in range(25):
        c = Classification.EXTERNAL if i < 4 else Classification.INTERNAL
        lm.record("BTCZAR", c)
    s1 = lm.should_alert("BTCZAR")
    assert s1 is not None
    s2 = lm.should_alert("BTCZAR")
    assert s2 is None  # cooldown
