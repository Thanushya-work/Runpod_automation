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

## One-time setup (manual steps)

1. **Rotate leaked credentials.** `config.json` and `bot_notifier.py` in your public repo contain real secrets
   (DB, S3, Telegram). Create a new Telegram bot token (@BotFather → `/revoke`), and change the DB and S3 passwords/keys.
   Remove them from the repo (and from its git history if they were ever pushed).
2. **Create a GitHub repo** and upload this folder. Recommended: **public** (it contains no secrets, and
   the 10-minute watcher would use up the free minutes of a private repo: ~4,300 min/month vs 2,000 free).
   If you must use private, change the watcher cron in `.github/workflows/watch.yml` to `*/20` or less often.
3. **Add Secrets** in the repo: Settings → Secrets and variables → Actions → *New repository secret*:

   | Secret | Value |
   |---|---|
   | `RUNPOD_API_KEY` | the key your admin created |
   | `TG_BOT_TOKEN` | the NEW bot token |
   | `TG_CHAT_IDS` | comma-separated chat ids, e.g. `111111111,222222222` (your colleague and you) |
   | `CONFIG_JSON_B64` | your production `config.json`, base64-encoded (see below) |

   Make `CONFIG_JSON_B64` in PowerShell, then paste it as the secret value:
   ```powershell
   [Convert]::ToBase64String([IO.File]::ReadAllBytes("config.json")) | Set-Clipboard
   ```
   Chat ids: each person sends any message to the bot, then asks @userinfobot for their id.
4. **(Optional) variables** (Settings → Secrets and variables → Actions → *Variables*):
   `REPO_REF` = a commit id of the pipeline repo to pin the exact code (otherwise it takes the latest
   `IOU-Dedup` every night, so an unreviewed push goes to production), `BATCH_LIMIT`, `WORKER_NAME` (default `pod-1`).
5. **Check the pipeline repo reads config from the repo root.** `bootstrap.sh` writes `config.json` to
   `/workspace/Grounding_Dino/config.json`. If `main.py` expects it elsewhere, change step 6 of `bootstrap.sh`.
6. **Make sure your Telegram change (multiple chat ids) is pushed** to the `IOU-Dedup` branch, since the pod clones from GitHub.
   Until then only the first id receives the pipeline's own messages (the automation's alerts reach everyone).

## Test it before trusting it (do these in order)

1. **Setup-only test** (does not touch your database or S3): Actions → *Launch daily pipeline pod* → *Run workflow* →
   tick `setup_only`, and set `gpu_override` to a GPU that is in stock (e.g. `NVIDIA A40`) so the test doesn't wait.
   You should get "🚀 Pipeline pod started". Wait ~15-30 min. Run *Watch pipeline pods* manually to see it detect `SETUP_DONE`/`PIPELINE_DONE`;
   after 15 minutes it sends "✅ finished" and deletes the pod.
   - If something goes wrong you'll get "Setup FAILED at step ..." with the last log lines.
2. **Check the log reading works.** On your computer (with `RUNPOD_API_KEY` set) while a test pod is up:
   `python watcher.py --show POD_ID`. It must show recent timestamped lines and "understood" values.
   If it shows nothing, the log API behaves differently from what I assumed: tell me what it prints.
3. **Small real run:** set the variable `BATCH_LIMIT` to a small number, run the workflow manually without `setup_only`.
   Check the results, then remove `BATCH_LIMIT`.
4. Only then leave the schedule on. After the first real nights, check the Runpod console that no pod was left behind.

## What you must know

- **A pod can still be left running** if the watcher can't read the log or the pod's markers never appear (e.g. the pod freezes
  without printing). You then get a "log quiet" alert after 1.5 h and a "running Nh" reminder after 6 h, but the pod is not touched.
  Delete it from the Runpod console if needed. If a delete itself fails you get an alert saying so.
- **Exit code 0 is not proof of success** (your pipeline catches many errors). Read the pipeline's own completion message.
- **Never auto-restart after a failure.** Rows stay marked "in progress" in the database; reconcile them first.
- **Setup runs every night** (about 10-20 min of paid time; not measured). If it proves slow or flaky, build a Docker image
  with everything preinstalled and set the `POD_IMAGE` environment variable.
- **GitHub timing:** scheduled runs can start a few minutes late. Public repos get their schedules **disabled after 60 days
  without repo activity**: GitHub emails you; re-enable it in the Actions tab (or push any small commit).
- **Secrets in Runpod:** `CONFIG_JSON_B64` and the Telegram token are passed to the pod as environment variables, so anyone with
  access to your Runpod account can see them in the pod details. Keep Runpod access limited.
- **Cost guide:** the three GPUs cost $0.25-0.28/hour on Secure Cloud; you pay from pod start until deletion (setup + pipeline + 15 min).
- **Image tag unverified:** `runpod/pytorch:2.5.1-py3.11-cuda12.1.1-devel-ubuntu22.04` is my best match for your requirements.
  If the setup-only test fails to start, set the `POD_IMAGE` environment variable (or edit `settings.py`).
- **Everything tunable is in `settings.py`** (GPU order, disk sizes, 1.5 h quiet limit, 15 min grace, retry window of 3 h).

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
