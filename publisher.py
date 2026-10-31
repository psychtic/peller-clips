#!/usr/bin/env python3
"""Publisher: runs after the clip robot.

For every clip waiting in queue.json it:
  1. checks Telegram for Reject taps
  2. if the clip was rejected: marks it rejected and tells you
  3. if the reject window (30 min) is over: downloads it back from Telegram
     and uploads it to YouTube, then tells you the link
  4. otherwise leaves it for the next run

Needs a bot that nothing else is reading updates from (see README).
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import requests

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
QUEUE_FILE = Path(os.environ.get("QUEUE_FILE", "queue.json"))
WINDOW_MINUTES = int(os.environ.get("WINDOW_MINUTES", "30"))
MAX_UPLOADS_PER_DAY = int(os.environ.get("MAX_UPLOADS_PER_DAY", "6"))
YT_PRIVACY = os.environ.get("YT_PRIVACY", "public")  # public, unlisted or private
YT_CLIENT_ID = os.environ.get("YT_CLIENT_ID", "")
YT_CLIENT_SECRET = os.environ.get("YT_CLIENT_SECRET", "")
YT_REFRESH_TOKEN = os.environ.get("YT_REFRESH_TOKEN", "")
TG_FILE_LIMIT = 20 * 1024 * 1024  # bots cannot fetch bigger files


def log(*a):
    print(*a, flush=True)


# --------------------------------------------------------------- telegram
def tg(method, **kw):
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}",
                      timeout=120, **kw)
    r.raise_for_status()
    return r.json()


def say(text):
    try:
        tg("sendMessage", data={"chat_id": TG_CHAT, "text": text,
                                "disable_web_page_preview": "true"})
    except Exception as e:
        log(f"telegram message failed: {e}")


def collect_rejections():
    """Return the set of clip ids you tapped Reject on, and clear those updates."""
    rejected, last_id = set(), None
    updates = tg("getUpdates", data={"timeout": 0,
                                     "allowed_updates": json.dumps(["callback_query"])})
    for u in updates.get("result", []):
        last_id = u["update_id"]
        cq = u.get("callback_query") or {}
        data = cq.get("data", "")
        if data.startswith("rej:"):
            rejected.add(data[4:])
            try:  # stops the button spinner; fails harmlessly if too old
                tg("answerCallbackQuery", data={"callback_query_id": cq["id"],
                                                "text": "Rejected"})
            except Exception:
                pass
    if last_id is not None:  # tell Telegram we have handled these
        tg("getUpdates", data={"offset": last_id + 1, "timeout": 0,
                               "allowed_updates": json.dumps(["callback_query"])})
    return rejected


def clear_button(message_id):
    try:
        tg("editMessageReplyMarkup", data={"chat_id": TG_CHAT, "message_id": message_id,
                                           "reply_markup": json.dumps({"inline_keyboard": []})})
    except Exception:
        pass


def fetch_video(file_id, dest):
    info = tg("getFile", data={"file_id": file_id})["result"]
    if info.get("file_size", 0) > TG_FILE_LIMIT:
        raise RuntimeError("video is over Telegram's 20MB limit for bots")
    r = requests.get(f"https://api.telegram.org/file/bot{TG_TOKEN}/{info['file_path']}",
                     timeout=180)
    r.raise_for_status()
    Path(dest).write_bytes(r.content)


# ---------------------------------------------------------------- youtube
def youtube_ready():
    return bool(YT_CLIENT_ID and YT_CLIENT_SECRET and YT_REFRESH_TOKEN)


def upload_to_youtube(path, title, description):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = Credentials(None, refresh_token=YT_REFRESH_TOKEN,
                        token_uri="https://oauth2.googleapis.com/token",
                        client_id=YT_CLIENT_ID, client_secret=YT_CLIENT_SECRET,
                        scopes=["https://www.googleapis.com/auth/youtube.upload"])
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    body = {"snippet": {"title": title[:100], "description": description[:4900],
                        "categoryId": "24"},          # 24 = Entertainment
            "status": {"privacyStatus": YT_PRIVACY,
                       "selfDeclaredMadeForKids": False}}
    media = MediaFileUpload(str(path), mimetype="video/mp4", resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return resp["id"]


# ------------------------------------------------------------------- main
def load_queue():
    try:
        q = json.loads(QUEUE_FILE.read_text())
        q.setdefault("items", [])
        return q
    except Exception:
        return {"items": []}


def main(now=None):
    now = now or time.time()
    if not (TG_TOKEN and TG_CHAT):
        log("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing")
        return 1

    q = load_queue()
    pending = [i for i in q["items"] if i["status"] == "pending"]
    if not pending:
        log("nothing waiting")
        return 0

    rejected = collect_rejections()
    published_today = sum(1 for i in q["items"] if i["status"] == "published"
                          and now - i.get("published_at", 0) < 86400)

    for item in pending:
        cid = item["id"]
        age_min = (now - item["sent_at"]) / 60
        if cid in rejected:
            item["status"] = "rejected"
            clear_button(item["message_id"])
            say(f"Rejected. Will not post: {item['title']}")
            log(f"{cid}: rejected")
        elif age_min < WINDOW_MINUTES:
            log(f"{cid}: still in reject window ({age_min:.0f} min old)")
        elif not youtube_ready():
            log(f"{cid}: YouTube not connected yet, leaving it")
            if not q.get("warned_no_youtube"):
                say("A clip finished its reject window, but YouTube is not connected "
                    "yet, so nothing was posted. Add the YT_ secrets (see README).")
                q["warned_no_youtube"] = True
        elif published_today >= MAX_UPLOADS_PER_DAY:
            item["status"] = "skipped_daily_limit"
            clear_button(item["message_id"])
            say(f"Daily upload limit ({MAX_UPLOADS_PER_DAY}) reached, skipped: {item['title']}")
        else:
            try:
                with tempfile.TemporaryDirectory() as d:
                    path = Path(d, "clip.mp4")
                    fetch_video(item["file_id"], path)
                    vid = upload_to_youtube(path, item["title"], item["description"])
                item.update(status="published", youtube_id=vid, published_at=int(now))
                published_today += 1
                clear_button(item["message_id"])
                say(f"Posted to YouTube ({YT_PRIVACY}): https://youtube.com/shorts/{vid}")
                log(f"{cid}: published {vid}")
            except Exception as e:
                item["status"] = "failed"
                say(f"Could not post '{item['title']}': {type(e).__name__}: {str(e)[:200]}")
                log(f"{cid}: failed {type(e).__name__}: {e}")

    QUEUE_FILE.write_text(json.dumps(q, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
