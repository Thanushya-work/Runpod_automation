# Runpod daily pipeline automation

Every night at **10:00 PM IST** this starts a Runpod pod on the first available of
**RTX A4500 → RTX A5000 → RTX 4000 Ada** (Secure Cloud, 50 GB + 50 GB), runs your setup commands and
`python main.py pod-1` on it, alerts you on Telegram, and **deletes the pod when the pipeline is done**.
No dedicated pod: it only exists while it works.

## How it works (the short version)

```
GitHub Actions, 22:00 IST            GitHub Actions, every 10 min          The pod
launcher.py                          watcher.py                            bootstrap.sh
 - checks which GPU is in stock       - reads the pod's log                 - installs, builds GroundingDINO
 - creates the pod (retries 3 h)      - done OK  -> wait 15 min, delete     - writes config.json from a secret
 - Telegram: "pod started"            - failed   -> alert, delete in 30 min - runs main.py
                                      - log quiet 1.5 h -> alert ONLY       - prints PIPELINE_DONE exit=<code>
                                      - never deletes a slow/quiet pod
```

The pod and the watcher talk only through the pod's log (every line is timestamped; the markers
`SETUP_FAILED step=...` and `PIPELINE_DONE exit=...` mean "setup broke" / "main.py ended").


## Files

| File | Purpose |
|---|---|
| `settings.py` | all settings |
| `runpod_client.py` | Runpod API v2 calls (with retries) |
| `launcher.py` | creates tonight's pod with GPU fallback and retries |
| `watcher.py` | monitors, alerts, deletes finished pods |
| `bootstrap.sh` | runs inside the pod: setup, then `main.py` |
| `notify.py` | Telegram to several chats |
| `.github/workflows/` | the 22:00 IST schedule and the 10-minute watcher |
| `tests/` | `pip install pytest && python -m pytest tests` |
