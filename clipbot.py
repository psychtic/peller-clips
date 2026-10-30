#!/usr/bin/env python3
"""Kick clip robot.

Finds a streamer's best new Kick clips, turns each into a captioned vertical
(9:16) video, writes a title + description, and sends it to your Telegram.

Usage:
  python clipbot.py                  normal run (what GitHub Actions does)
  python clipbot.py --check          only test that Kick can be reached
  python clipbot.py --test-video in.mp4 out.mp4   build one video locally
"""
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

try:  # looks like a real browser, which helps against Kick's bot protection
    from curl_cffi import requests as kick_http
    KICK_KWARGS = {"impersonate": "chrome"}
except ImportError:  # still works, but Kick is more likely to refuse
    kick_http = requests
    KICK_KWARGS = {}

# ---------------------------------------------------------------- settings
CHANNEL = os.environ.get("KICK_CHANNEL", "peller")       # kick.com/<this>
CREDIT = os.environ.get("CREDIT_NAME", "Peller")         # name shown in video
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "2"))    # videos per run
MIN_SECONDS = int(os.environ.get("MIN_SECONDS", "12"))   # skip tiny clips
MAX_AGE_HOURS = int(os.environ.get("MAX_AGE_HOURS", "48"))
CAPTIONS = os.environ.get("CAPTIONS", "1") == "1"
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base")
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"          # build but don't send
STATE_FILE = Path(os.environ.get("STATE_FILE", "seen.json"))
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-5-5")

FONT_CANDIDATES = [
    os.environ.get("FONT_FILE", ""),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def log(*a):
    print(*a, flush=True)


# ------------------------------------------------------------------- state
def load_seen():
    try:
        return set(json.loads(STATE_FILE.read_text()))
    except Exception:
        return set()


def save_seen(seen):
    STATE_FILE.write_text(json.dumps(sorted(seen), indent=1))


# ------------------------------------------------------------------- kick
def fetch_clips():
    """Return a list of raw clip dicts from Kick's (unofficial) public API."""
    urls = [
        f"https://kick.com/api/v2/channels/{CHANNEL}/clips?sort=view&time=week",
        f"https://kick.com/api/v2/channels/{CHANNEL}/clips?sort=date",
    ]
    last_error = "no response"
    for url in urls:
        try:
            r = kick_http.get(url, timeout=30, headers={"Accept": "application/json"},
                              **KICK_KWARGS)
            if r.status_code != 200:
                last_error = f"HTTP {r.status_code} from Kick"
                continue
            data = r.json()
            clips = data.get("clips") if isinstance(data, dict) else data
            if clips:
                return clips
            last_error = "Kick answered but returned no clips"
        except Exception as e:  # network error, blocked, bad JSON
            last_error = f"{type(e).__name__}: {e}"
    raise RuntimeError(last_error)


def parse_clip(c):
    """Pull out the fields we need, tolerating small API changes."""
    def num(*keys):
        for k in keys:
            try:
                if c.get(k) is not None:
                    return float(c[k])
            except (TypeError, ValueError):
                pass
        return 0.0

    created = c.get("created_at") or c.get("createdAt") or ""
    try:
        when = datetime.fromisoformat(created.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
    except Exception:
        when = datetime.now(timezone.utc)
    return {
        "id": str(c.get("id")),
        "title": (c.get("title") or "").strip(),
        "views": num("view_count", "views"),
        "duration": num("duration", "clip_duration"),
        "created": when,
        "media": c.get("clip_url") or c.get("video_url") or c.get("url") or "",
    }


def pick_clips(raw, seen):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    clips = [parse_clip(c) for c in raw]
    good = [c for c in clips
            if c["id"] not in seen and c["media"] and c["created"] >= cutoff
            and (c["duration"] == 0 or c["duration"] >= MIN_SECONDS)]
    good.sort(key=lambda c: c["views"], reverse=True)
    return good[:MAX_PER_RUN]


def download(media_url, out_path):
    """ffmpeg handles both .mp4 and .m3u8 links."""
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", media_url,
                    "-c", "copy", "-t", "75", str(out_path)],
                   check=True, timeout=240)


# --------------------------------------------------------------- captions
def srt_time(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def transcribe(video_path, srt_path):
    """Make an .srt with Whisper. Returns True on success, False to skip captions."""
    if not CAPTIONS:
        return False
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        segments, _ = model.transcribe(str(video_path), vad_filter=True)
        lines, n = [], 0
        for seg in segments:
            text = seg.text.strip()
            if not text:
                continue
            n += 1
            lines.append(f"{n}\n{srt_time(seg.start)} --> {srt_time(seg.end)}\n{text}\n")
        if not n:
            return False
        Path(srt_path).write_text("\n".join(lines), encoding="utf-8")
        return True
    except Exception as e:
        log(f"  captions skipped: {type(e).__name__}: {e}")
        return False


# ------------------------------------------------------------------ video
def find_font():
    for f in FONT_CANDIDATES:
        if f and Path(f).exists():
            return f
    return ""


def build_video(src, out, credit, srt=None):
    """Vertical 1080x1920: blurred background, clip in the middle, credit on top,
    captions at the bottom. Runs inside the folder that holds src/srt so the
    filter paths stay simple."""
    src, out = Path(src).resolve(), Path(out).resolve()
    work = src.parent
    (work / "credit.txt").write_text(f"{credit} on Kick", encoding="utf-8")
    font = find_font()

    chain = ("[0:v]split[a][b];"
             "[a]scale=1080:1920:force_original_aspect_ratio=increase,"
             "crop=1080:1920,boxblur=28:6[bg];"
             "[b]scale=1080:-2[fg];"
             "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[v0]")
    last = "v0"
    if font:
        chain += (f";[{last}]drawtext=fontfile={font}:textfile=credit.txt:"
                  "fontsize=46:fontcolor=white:borderw=4:bordercolor=black:"
                  "x=(w-text_w)/2:y=150[v1]")
        last = "v1"
    if srt:
        # SRT captions are laid out on a small 384x288 grid, so these numbers are
        # not pixels. On the 1080x1920 video, 1 unit is about 6.7 pixels:
        # FontSize 10 ~ 67px text, MarginV 65 ~ 430px up from the bottom.
        style = ("FontName=DejaVu Sans,Bold=1,FontSize=10,PrimaryColour=&H00FFFFFF,"
                 "OutlineColour=&H00000000,BorderStyle=1,Outline=1,Shadow=0,"
                 "Alignment=2,MarginV=65,MarginL=9,MarginR=9")
        chain += f";[{last}]subtitles={Path(srt).name}:force_style='{style}'[v2]"
        last = "v2"

    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", src.name,
           "-filter_complex", chain, "-map", f"[{last}]", "-map", "0:a?",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
           "-movflags", "+faststart", "-t", "60", str(out)]
    subprocess.run(cmd, check=True, cwd=work, timeout=600)


# ------------------------------------------------------------ title & text
def make_title(clip):
    page = f"https://kick.com/{CHANNEL}/clips/{clip['id']}"
    fallback_title = (clip["title"] or f"{CREDIT} stream moment")[:70]
    desc = (f"Clip from {CREDIT}'s Kick stream. All credit to {CREDIT}.\n"
            f"Watch live: https://kick.com/{CHANNEL}\nOriginal clip: {page}\n"
            f"#{CHANNEL} #kick #nigeria #shorts")
    if ANTHROPIC_KEY:
        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": ANTHROPIC_MODEL, "max_tokens": 300, "messages": [{
                    "role": "user",
                    "content": (
                        f"Write a YouTube Shorts title (max 70 characters, no quotes, "
                        f"curiosity-driven, must not mislead) for a clip of Nigerian "
                        f"streamer {CREDIT} on Kick. The clip is called: "
                        f"'{clip['title']}'. Reply with only the title.")}]},
                timeout=40)
            r.raise_for_status()
            t = r.json()["content"][0]["text"].strip().strip('"')
            if t:
                fallback_title = t[:90]
        except Exception as e:
            log(f"  title via Claude skipped: {type(e).__name__}")
    return fallback_title, desc


# --------------------------------------------------------------- telegram
def tg(method, **kw):
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}",
                      timeout=180, **kw)
    r.raise_for_status()


def notify(text):
    if TG_TOKEN and TG_CHAT and not DRY_RUN:
        try:
            tg("sendMessage", data={"chat_id": TG_CHAT, "text": text})
        except Exception as e:
            log(f"telegram notify failed: {e}")


def send_video(path, title, desc):
    caption = f"{title}\n\n{desc}"[:1000]
    with open(path, "rb") as f:
        tg("sendVideo", data={"chat_id": TG_CHAT, "caption": caption,
                              "supports_streaming": "true"},
           files={"video": f})


# ------------------------------------------------------------------- main
def main():
    if len(sys.argv) >= 4 and sys.argv[1] == "--test-video":
        build_video(sys.argv[2], sys.argv[3], CREDIT,
                    srt=sys.argv[4] if len(sys.argv) > 4 else None)
        log("built", sys.argv[3])
        return 0

    try:
        raw = fetch_clips()
    except Exception as e:
        msg = f"Clip robot could not read Kick clips for '{CHANNEL}': {e}"
        log(msg)
        notify(msg)
        return 1
    log(f"Kick reachable: {len(raw)} clips returned for '{CHANNEL}'")
    if "--check" in sys.argv:
        return 0

    if not DRY_RUN and not (TG_TOKEN and TG_CHAT):
        log("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing")
        return 1

    seen = load_seen()
    chosen = pick_clips(raw, seen)
    log(f"{len(chosen)} new clip(s) to process")
    for clip in chosen:
        log(f"- {clip['id']} ({int(clip['views'])} views) {clip['title']}")
        try:
            with tempfile.TemporaryDirectory() as d:
                src, srt, out = Path(d, "in.mp4"), Path(d, "captions.srt"), Path(d, "out.mp4")
                download(clip["media"], src)
                has_srt = transcribe(src, srt)
                build_video(src, out, CREDIT, srt=srt if has_srt else None)
                title, desc = make_title(clip)
                if DRY_RUN:
                    keep = Path(f"dryrun_{clip['id']}.mp4")
                    keep.write_bytes(out.read_bytes())
                    log(f"  dry run saved {keep}")
                else:
                    send_video(out, title, desc)
                    log("  sent to Telegram")
            seen.add(clip["id"])
            save_seen(seen)
        except Exception as e:
            log(f"  failed: {type(e).__name__}: {e}")
            notify(f"Clip robot failed on clip {clip['id']}: {type(e).__name__}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
