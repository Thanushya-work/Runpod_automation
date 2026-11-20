import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import settings as S
from runpod_client import normalize_logs
from watcher import analyze_logs, decide

NOW = datetime(2026, 11, 17, 18, 0, 0, tzinfo=timezone.utc)


def stamp(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def pod(status="RUNNING", started_min_ago=120):
    return {"id": "p1", "name": "gdino-pipeline-20261117", "status": status,
            "startedAt": (NOW - timedelta(minutes=started_min_ago)).isoformat()}


def kinds(acts):
    return [a[0] for a in acts]


# ---------- log parsing ----------
def test_analyze_basic():
    text = f"{stamp(30)} hello\n{stamp(10)} processing store 5\n"
    info = analyze_logs(text)
    assert info.last_ts == datetime.strptime(stamp(10), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert info.done_exit is None and info.setup_failed is None


def test_analyze_done_and_failed_markers():
    text = f"{stamp(40)} x\n{stamp(20)} PIPELINE_DONE exit=1\n{stamp(25)} SETUP_FAILED step=pip-requirements\n"
    info = analyze_logs(text)
    assert info.done_exit == 1 and info.setup_failed == "pip-requirements"


def test_analyze_tolerates_foreign_prefix():
    text = f"2026-11-17T17:00:00Z {stamp(20)} PIPELINE_DONE exit=0\n"
    assert analyze_logs(text).done_exit == 0


def test_normalize_sse_and_json():
    assert normalize_logs("data: a\ndata: b") == "a\nb"
    assert "PIPELINE_DONE" in normalize_logs('{"logs": ["x", "PIPELINE_DONE exit=0"]}')


# ---------- decisions ----------
def test_active_log_no_action():
    info = analyze_logs(f"{stamp(5)} working")
    assert decide(pod(), info, NOW, {}) == []


def test_quiet_log_alerts_but_never_terminates_and_is_throttled():
    info = analyze_logs(f"{stamp(S.STUCK_QUIET_MINUTES + 5)} last line")
    st = {}
    acts = decide(pod(), info, NOW, st)
    assert kinds(acts) == ["alert"]
    assert decide(pod(), info, NOW + timedelta(minutes=10), st) == []          # throttled
    again = decide(pod(), info, NOW + timedelta(minutes=S.STUCK_REALERT_MINUTES + 1), st)
    assert kinds(again) == ["alert"]                                            # re-alerts later


def test_long_pipeline_not_killed_even_after_many_hours():
    info = analyze_logs(f"{stamp(2)} still going")
    acts = decide(pod(started_min_ago=600), info, NOW, {})
    assert "terminate" not in kinds(acts)


def test_done_ok_waits_for_grace_then_terminates():
    early = analyze_logs(f"{stamp(5)} PIPELINE_DONE exit=0")
    assert decide(pod(), early, NOW, {}) == []
    late = analyze_logs(f"{stamp(S.DONE_GRACE_MINUTES + 1)} PIPELINE_DONE exit=0")
    acts = decide(pod(), late, NOW, {})
    assert kinds(acts) == ["alert", "terminate"]


def test_done_failure_alerts_immediately_keeps_pod_then_terminates():
    early = analyze_logs(f"{stamp(2)} PIPELINE_DONE exit=1")
    st = {}
    acts = decide(pod(), early, NOW, st)
    assert kinds(acts) == ["alert"]
    assert decide(pod(), early, NOW + timedelta(minutes=5), st) == []           # no repeat alert
    late = analyze_logs(f"{stamp(S.FAILED_KEEP_MINUTES + 1)} PIPELINE_DONE exit=1")
    assert "terminate" in kinds(decide(pod(), late, NOW, {"fail_alerted": True}))


def test_setup_failed_alert_then_terminate():
    fresh = analyze_logs(f"{stamp(1)} SETUP_FAILED step=groundingdino-build")
    st = {}
    assert kinds(decide(pod(), fresh, NOW, st)) == ["alert"]
    old = analyze_logs(f"{stamp(S.FAILED_KEEP_MINUTES + 1)} SETUP_FAILED step=groundingdino-build")
    assert "terminate" in kinds(decide(pod(), old, NOW, st))


def test_exited_pod_alerts_and_terminates():
    acts = decide(pod(status="EXITED"), analyze_logs(""), NOW, {})
    assert kinds(acts) == ["alert", "terminate"]


def test_slow_start_alert():
    acts = decide(pod(status="STARTING", started_min_ago=S.START_TIMEOUT_MINUTES + 5), analyze_logs(""), NOW, {})
    assert kinds(acts) == ["alert"]


def test_unreadable_logs_use_start_time_not_instant_alarm():
    # logs unavailable right after start: no alert until the quiet threshold passes
    assert decide(pod(started_min_ago=10), analyze_logs(""), NOW, {}) == []


def test_runpod_json_line_stream():
    """Real Runpod log stream format: SSE 'id:' lines + one JSON record per line."""
    raw = (
        'id: 2026-11-19T23:21:40.1Z\n'
        '{"source":"container","line":"2026-11-19T23:21:40Z ImportError: libc10.so: cannot open shared object file","ts":"2026-11-19T23:21:40.1Z"}\n'
        'id: 2026-11-19T23:21:47.379136176Z\n'
        '{"source":"container","line":"2026-11-19T23:21:47Z SETUP_FAILED step=groundingdino-import","ts":"2026-11-19T23:21:47.379136176Z"}\n'
    )
    info = analyze_logs(normalize_logs(raw))
    assert info.setup_failed == "groundingdino-import"
    assert info.setup_failed_ts == datetime(2026, 11, 19, 23, 21, 47, tzinfo=timezone.utc)
    assert "libc10.so" in info.tail and "id:" not in info.tail and '"source"' not in info.tail
