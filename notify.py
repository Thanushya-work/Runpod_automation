"""Telegram alerts to one or more chats. Never prints the bot token."""
import os
import requests


def chat_ids():
    raw = os.environ.get("TG_CHAT_IDS") or os.environ.get("TG_CHAT_ID", "")
    return [c.strip() for c in raw.split(",") if c.strip()]


def send(text):
    token = os.environ.get("TG_BOT_TOKEN")
    ids = chat_ids()
    if not token or not ids:
        print("Telegram not configured; message not sent.")
        return False
    ok = True
    for cid in ids:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": cid, "text": text[:3900]},
                timeout=20,
            )
            if not r.ok:
                ok = False
                print(f"Telegram send to one chat failed: HTTP {r.status_code}")
        except requests.RequestException as e:
            ok = False
            # type only: the exception text would contain the URL, which contains the token
            print(f"Telegram send to one chat failed: {type(e).__name__}")
    return ok
