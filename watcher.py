"""
Supervisor, run every ~10 minutes. For each of our pods it reads the pod log and:
  * pipeline finished OK     -> wait the grace period, send "done", delete the pod
  * pipeline/setup failed    -> alert, keep the pod 30 min for inspection, then delete it
  * log quiet for 1.5 hours  -> alert (the pod is NEVER deleted for being slow or quiet)
  * pod not starting / pod crashed -> alert
It never prints log contents (GitHub workflow logs of a public repo are public); logs go only to Telegram.

    python watcher.py                # normal run (state kept in state.json)
    python watcher.py --show POD_ID  # debugging: print the log tail and what the watcher understands
"""
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import notify
import settings as S
from runpod_client import Runpod, RunpodError

STATE_FILE = os.environ.get("STATE_FILE", "state.json")
TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)")
DONE_RE = re.compile(r"PIPELINE_DONE exit=(\d+)")
FAIL_RE = re.compile(r"SETUP_FAILED step=(\S+)")


@dataclass
class LogInfo:
    last_ts: Optional[datetime] = None
    done_exit: Optional[int] = None
    done_ts: Optional[datetime] = None
    setup_failed: Optional[str] = None
    setup_failed_ts: Optional[datetime] = None
    tail: str = ""


def _ts(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _dt(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def analyze_logs(text):
    info = LogInfo()
    lines = [l for l in text.splitlines() if l.strip()]
    for line in lines:
        stamps = TS_RE.findall(line)
        ts = _ts(stamps[-1]) if stamps else None
        if ts and (info.last_ts is None or ts > info.last_ts):
            info.last_ts = ts
        m = DONE_RE.search(line)
        if m:
            info.done_exit, info.done_ts = int(m.group(1)), ts
        m = FAIL_RE.search(line)
        if m:
            info.setup_failed, info.setup_failed_ts = m.group(1), ts
    info.tail = "\n".join(l[:200] for l in lines[-15:])
    return info


def decide(pod, info, now, st):
    """Pure decision logic. Returns [("alert", text, with_tail) | ("terminate", reason, False)].
    Mutates `st` (per-pod memory used to avoid repeating alerts)."""
    acts = []
    status = pod.get("status")
    started = _dt(pod.get("startedAt")) or _dt(pod.get("createdAt")) or now
    age = now - started

    def recently(key, minutes):
        last = _dt(st.get(key))
        return last is not None and now - last < timedelta(minutes=minutes)

    def mark(key):
        st[key] = now.isoformat()

    if status in ("ERROR", "EXITED"):
        acts.append(("alert", f"❌ Pod is {status} unexpectedly. Deleting it. Last log lines below.", True))
        acts.append(("terminate", f"pod {status}", False))
        return acts

    if status != "RUNNING":                      # PROVISIONING / STARTING
        if age > timedelta(minutes=S.START_TIMEOUT_MINUTES) and not recently("start_alert", S.STUCK_REALERT_MINUTES):
            acts.append(("alert", f"⚠️ Pod has been {status} for {int(age.total_seconds() // 60)} min and is not running.", False))
            mark("start_alert")
        return acts

    # ---- RUNNING ----
    if info.setup_failed:
        if not st.get("fail_alerted"):
            acts.append(("alert", f"❌ Setup FAILED at step '{info.setup_failed}'. Pipeline did not run. "
                                  f"Pod kept {S.FAILED_KEEP_MINUTES} min for inspection, then deleted.", True))
            st["fail_alerted"] = True
        if now - (info.setup_failed_ts or now) >= timedelta(minutes=S.FAILED_KEEP_MINUTES):
            acts.append(("terminate", "setup failed", False))
        return acts

    if info.done_exit is not None:
        ok = info.done_exit == 0
        grace = S.DONE_GRACE_MINUTES if ok else S.FAILED_KEEP_MINUTES
        if not ok and not st.get("fail_alerted"):
            acts.append(("alert", f"❌ main.py exited with code {info.done_exit}. Do NOT just restart: check rows left "
                                  f"in-progress in the database first. Pod kept {grace} min, then deleted.", True))
            st["fail_alerted"] = True
        if now - (info.done_ts or now) >= timedelta(minutes=grace):
            if ok:
                acts.append(("alert", "✅ Pipeline finished (exit 0). Pod deleted. Note: exit 0 does not guarantee "
                                      "every stage succeeded; see the pipeline's own completion message.", True))
            acts.append(("terminate", "pipeline finished", False))
        return acts

    # still running: only ever alert, never delete
    reference = info.last_ts or started
    quiet = now - reference
    if quiet >= timedelta(minutes=S.STUCK_QUIET_MINUTES) and not recently("stuck_alert", S.STUCK_REALERT_MINUTES):
        what = ("No log line for" if info.last_ts else "No readable log lines since start,")
        acts.append(("alert", f"⚠️ {what} {int(quiet.total_seconds() // 60)} min. Pod is still running and has NOT "
                              "been touched. It may be stuck or just in a long step.", True))
        mark("stuck_alert")
    if age >= timedelta(hours=S.LONG_RUNNING_HOURS) and not recently("long_alert", S.LONG_RUNNING_REALERT_MINUTES):
        acts.append(("alert", f"ℹ️ Pod has been running {int(age.total_seconds() // 3600)}h (reminder only, not touched).", False))
        mark("long_alert")
    return acts


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def run():
    client = Runpod(os.environ.get("RUNPOD_API_KEY"))
    now = datetime.now(timezone.utc)
    state = load_state()
    problems = 0
    try:
        pods = [p for p in client.list_pods()
                if p.get("name", "").startswith(S.NAME_PREFIX) and p.get("status") != "TERMINATED"]
    except RunpodError as e:
        print("Could not list pods:", e)
        return 1
    state = {k: v for k, v in state.items() if k in {p["id"] for p in pods}}   # forget deleted pods
    print(f"{len(pods)} pipeline pod(s) active")

    for pod in pods:
        pid, name = pod["id"], pod.get("name", "")
        st = state.setdefault(pid, {})
        logs = client.get_logs(pid) if pod.get("status") in ("RUNNING", "EXITED", "ERROR") else ""
        info = analyze_logs(logs)
        for kind, text, with_tail in decide(pod, info, now, st):
            if kind == "alert":
                msg = f"[{name}] {text}"
                if with_tail and info.tail:
                    msg += "\n\n" + info.tail
                notify.send(msg)
                print(f"{pid}: alert sent")
            else:
                try:
                    client.terminate(pid)
                    print(f"{pid}: terminated ({text})")
                    state.pop(pid, None)
                except RunpodError as e:
                    problems += 1
                    notify.send(f"❌ [{name}] Could not delete pod {pid}: {e}. It may still be billing, "
                                "please delete it in the Runpod console.")
    save_state(state)
    return 1 if problems else 0


def show(pod_id):
    client = Runpod(os.environ.get("RUNPOD_API_KEY"))
    logs = client.get_logs(pod_id)
    info = analyze_logs(logs)
    print("--- last log lines ---\n" + info.tail)
    print("--- understood ---")
    print({k: str(v) for k, v in info.__dict__.items() if k != "tail"})


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--show":
        show(sys.argv[2])
    else:
        sys.exit(run())
