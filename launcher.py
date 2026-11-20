"""
Daily launcher. Finds a free GPU (A4500 -> A5000 -> RTX 4000 Ada), creates the pod, and exits.
The pod then sets itself up and runs the pipeline on its own; watcher.py supervises it.

    python launcher.py                         # normal daily run
    python launcher.py --setup-only            # test: build the environment, do NOT run main.py
    python launcher.py --gpu "NVIDIA A40"      # test: use this GPU only

Exit code 0 = pod launched (or an earlier pod is still running); 1 = could not launch.
"""
import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import notify
import settings as S
from runpod_client import Runpod, RunpodError

IST = timezone(timedelta(hours=5, minutes=30))

# Passed from GitHub Secrets / the environment into the pod. Never printed.
PASS_THROUGH = ["CONFIG_JSON_B64", "TG_BOT_TOKEN", "TG_CHAT_IDS", "TG_CHAT_ID",
                "BATCH_LIMIT", "WORKER_NAME", "REPO_URL", "REPO_BRANCH", "REPO_REF"]


class Fatal(Exception):
    """A problem that retrying or another GPU cannot fix."""


def build_body(gpu_id, name, setup_only):
    import json
    env = {k: os.environ[k] for k in PASS_THROUGH if os.environ.get(k)}
    if "TG_CHAT_ID" not in env and env.get("TG_CHAT_IDS"):
        env["TG_CHAT_ID"] = env["TG_CHAT_IDS"].split(",")[0].strip()   # compatibility with old bot_notifier
    if setup_only:
        env["SETUP_ONLY"] = "1"
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "bootstrap.sh")) as f:
        script = f.read()
    return {
        "name": name,
        "image": S.IMAGE,
        "cloud": "SECURE",
        "gpu": {"id": gpu_id, "count": 1, "minCudaVersion": S.MIN_CUDA_VERSION},
        "disk": S.CONTAINER_DISK_GB,
        "mounts": {"persistent": {"size": S.VOLUME_DISK_GB, "path": S.MOUNT_PATH}},
        "env": env,
        "args": json.dumps({"entrypoint": ["/bin/bash", "-c"], "cmd": [script]}),
    }


def resolve_gpu(client, wanted):
    """Accept a GPU id ('NVIDIA RTX A5000') or display name ('RTX A5000', any case); return the id."""
    try:
        gpus = client.catalog_raw(min_cuda=None).json().get("gpus", [])
    except (RunpodError, ValueError):
        return wanted
    w = wanted.strip().lower()
    for g in gpus:
        if w in (str(g.get("id", "")).lower(), str(g.get("name", "")).lower()):
            return g["id"]
    raise Fatal(f"Unknown GPU '{wanted}'. Use a name/id from: python runpod_client.py --all")


def try_once(client, gpus, name, setup_only, explicit=False):
    """One pass over the GPUs. Returns the created pod, or None if nothing was available.
    explicit=True (GPU chosen by hand): always attempt it, Runpod itself says if it cannot place it."""
    try:
        stock = client.gpu_stock()
        candidates = [g for g in gpus if explicit or stock.get(g, "NONE") != "NONE"]
    except RunpodError:
        candidates = list(gpus)          # catalog unavailable: just try them all
    for gpu in candidates:
        r = client.create_pod(build_body(gpu, name, setup_only))
        if r.status_code == 201:
            return r.json()
        try:
            detail = r.json().get("detail", "")
        except ValueError:
            detail = r.text[:200]
        print(f"{gpu}: HTTP {r.status_code} {detail}")
        if r.status_code == 402:
            raise Fatal("Runpod balance is too low to create a pod.")
        if r.status_code == 401:
            raise Fatal("Runpod API key was rejected (401).")
        if r.status_code == 422:
            raise Fatal(f"Runpod rejected the pod request (422): {detail}")
        # 400 = no capacity / cannot place, 403 = no access to this pool: try the next GPU
    return None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup-only", action="store_true")
    ap.add_argument("--gpu")
    args = ap.parse_args(argv)

    client = Runpod(os.environ.get("RUNPOD_API_KEY"))
    name = f"{S.NAME_PREFIX}-{datetime.now(IST):%Y%m%d}"
    gpus = S.GPU_PREFERENCE

    try:
        if args.gpu:
            gpus = [resolve_gpu(client, args.gpu)]
            print("GPU override:", gpus[0])
        existing = [p for p in client.list_pods()
                    if p.get("name", "").startswith(S.NAME_PREFIX) and p.get("status") != "TERMINATED"]
        if existing:
            names = ", ".join(f'{p["name"]} ({p["id"]}, {p["status"]})' for p in existing)
            notify.send(f"⚠️ Daily launch skipped: an earlier pipeline pod is still active: {names}. "
                        "Check it; nothing was started.")
            print("Existing pod found; skipping launch.")
            return 0

        deadline = time.time() + S.LAUNCH_GIVE_UP_MINUTES * 60
        first = True
        while True:
            pod = try_once(client, gpus, name, args.setup_only, explicit=bool(args.gpu))
            if pod:
                notify.send(f"🚀 Pipeline pod started: {pod['gpu']['id']} in {pod.get('dataCenterId')} "
                            f"(${pod.get('cost')}/hr), pod id {pod['id']}"
                            + (" [SETUP-ONLY TEST]" if args.setup_only else ""))
                print("Launched pod", pod["id"])
                return 0
            if time.time() >= deadline:
                notify.send(f"❌ No GPU ({', '.join(gpus)}) became available in "
                            f"{S.LAUNCH_GIVE_UP_MINUTES} minutes. Tonight's pipeline did NOT start.")
                return 1
            if first:
                notify.send("⏳ No GPU free right now. Retrying every "
                            f"{S.LAUNCH_RETRY_SECONDS // 60} min for up to {S.LAUNCH_GIVE_UP_MINUTES} min.")
                first = False
            time.sleep(S.LAUNCH_RETRY_SECONDS)

    except Fatal as e:
        notify.send(f"❌ Launch failed: {e}")
        return 1
    except RunpodError as e:
        notify.send(f"❌ Launch failed (Runpod API problem): {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
