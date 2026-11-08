#!/usr/bin/env python3
"""Live clip catcher.

Watches a streamer's Kick channel. While they are live it records the stream in
short pieces, listens for sudden loud moments (shouting, laughing, hype), cuts
that moment out, and sends it through the same steps as the normal clip robot:
captions -> vertical video -> Telegram with a Reject button -> YouTube.

Usage:
  python livebot.py --check-live      is the channel live? (writes live=true/false
                                      for GitHub Actions, exits right away)
  python livebot.py                   normal run: watch while live, catch moments
  SOURCE_URL=some.mp4 python livebot.py   test on a local video instead of Kick
"""
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

import clipbot
from clipbot import CHANNEL, CREDIT, QUEUE_FILE, log

# ---------------------------------------------------------------- settings
SEG_SECONDS = int(os.environ.get("SEG_SECONDS", "6"))          # length of each recorded piece
PRE_SEGS = int(os.environ.get("PRE_SEGS", "5"))                # pieces kept BEFORE the moment
POST_SEGS = int(os.environ.get("POST_SEGS", "4"))              # pieces kept AFTER the moment
SPIKE_DB = float(os.environ.get("SPIKE_DB", "7"))              # how much louder than normal counts
MIN_LOUD_DB = float(os.environ.get("MIN_LOUD_DB", "-32"))      # ignore moments quieter than this
BASELINE_SEGS = int(os.environ.get("BASELINE_SEGS", "40"))     # pieces that define "normal" (~4 min)
WARMUP_SEGS = int(os.environ.get("WARMUP_SEGS", "15"))         # listen this long before catching
COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", "420"))
MAX_LIVE_CLIPS = int(os.environ.get("MAX_LIVE_CLIPS", "4"))    # per run
MAX_WATCH_MINUTES = int(os.environ.get("MAX_WATCH_MINUTES", "340"))
KEEP_SEGS = int(os.environ.get("KEEP_SEGS", "120"))            # how many pieces stay on disk
REQUIRE_SPEECH = os.environ.get("LIVE_REQUIRE_SPEECH", "1") == "1"
WINDOW_MINUTES = clipbot.WINDOW_MINUTES
SOURCE_URL = os.environ.get("SOURCE_URL", "")                  # testing: a local file
ANTHROPIC_KEY = clipbot.ANTHROPIC_KEY


# -------------------------------------------------------------------- kick
def kick_live():
    """Return (is_live, playback_url, stream_title). Raises if Kick cannot be read."""
    r = clipbot.kick_http.get(f"https://kick.com/api/v2/channels/{CHANNEL}", timeout=30,
                              headers={"Accept": "application/json"}, **clipbot.KICK_KWARGS)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} from Kick")
    data = r.json()
    live = data.get("livestream")
    if not live:
        return False, "", ""
    url = data.get("playback_url") or live.get("playback_url") or ""
    title = (live.get("session_title") or "").strip()
    return True, url, title


def check_live_mode():
    """For the first step of the GitHub workflow: cheap and quiet."""
    live = False
    try:
        live, url, title = kick_live()
        log(f"{CHANNEL} is {'LIVE: ' + title if live else 'offline'}")
    except Exception as e:
        log(f"could not check Kick: {type(e).__name__}: {e}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"live={'true' if live else 'false'}\n")
    return 0


# --------------------------------------------------------------- recording
def start_recorder(url, seg_dir, start_number):
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    if url.startswith("http"):
        cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10"]
    cmd += ["-i", url, "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy",
            "-f", "segment", "-segment_time", str(SEG_SECONDS),
            "-segment_start_number", str(start_number), "-reset_timestamps", "1",
            str(Path(seg_dir) / "seg_%06d.ts")]
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def seg_path(seg_dir, i):
    return Path(seg_dir) / f"seg_{i:06d}.ts"


def segment_level(path):
    """Average loudness of one piece in dB (closer to 0 = louder). Silence ~ -91."""
    try:
        p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn",
                            "-af", "volumedetect", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=90)
        m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", p.stderr)
        return float(m.group(1)) if m else -91.0
    except Exception:
        return -91.0


def is_moment(levels, idx):
    """True when the last 2 pieces (~12 s) are clearly louder than the recent normal."""
    if idx < WARMUP_SEGS:
        return False, 0.0, 0.0
    recent = [levels[i] for i in (idx - 1, idx) if i in levels]
    base = [levels[i] for i in range(max(0, idx - BASELINE_SEGS - 1), idx - 1) if i in levels]
    if len(recent) < 2 or len(base) < 10:
        return False, 0.0, 0.0
    now, normal = sum(recent) / len(recent), statistics.median(base)
    return (now >= MIN_LOUD_DB and now - normal >= SPIKE_DB), now, normal


# ------------------------------------------------------------ titles & text
def live_text(stream_title, transcript):
    fallback = f"{CREDIT} live moment"
    if stream_title:
        fallback = f"{CREDIT} live: {stream_title}"[:70]
    desc = (f"Live moment from {CREDIT}'s Kick stream. All credit to {CREDIT}.\n"
            f"Watch live: https://kick.com/{CHANNEL}\n"
            f"#{CHANNEL} #kick #nigeria #shorts")
    if ANTHROPIC_KEY:
        try:
            said = transcript[:500] if transcript else "(not available)"
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": clipbot.ANTHROPIC_MODEL, "max_tokens": 300, "messages": [{
                    "role": "user",
                    "content": (
                        f"Write a YouTube Shorts title (max 70 characters, no quotes, "
                        f"curiosity-driven, must not mislead) for a live moment of Nigerian "
                        f"streamer {CREDIT} on Kick. Stream title: '{stream_title}'. "
                        f"Words spoken in the clip (may contain errors): {said}. "
                        f"Reply with only the title.")}]},
                timeout=40)
            r.raise_for_status()
            t = r.json()["content"][0]["text"].strip().strip('"')
            if t:
                fallback = t[:90]
        except Exception as e:
            log(f"  title via Claude skipped: {type(e).__name__}")
    return fallback, desc


# ----------------------------------------------------------------- cutting
def make_clip(seg_dir, first, last, stream_title):
    """Cut pieces first..last into one captioned vertical video and send it.
    Returns the queue item, or None if skipped."""
    clip_id = f"live_{int(time.time())}"
    pieces = [seg_path(seg_dir, i) for i in range(first, last + 1) if seg_path(seg_dir, i).exists()]
    if len(pieces) < 3:
        log("  too few pieces on disk, skipping")
        return None
    with tempfile.TemporaryDirectory() as d:
        listing = Path(d, "list.txt")
        listing.write_text("".join(f"file '{p.resolve()}'\n" for p in pieces))
        src, ass, out = Path(d, "in.ts"), Path(d, "captions.ass"), Path(d, "out.mp4")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                        "-i", str(listing), "-c", "copy", str(src)], check=True, timeout=180)
        clipbot.LAST_TRANSCRIPT = ""
        has_captions = clipbot.transcribe(src, ass)
        if REQUIRE_SPEECH and not has_captions:
            log("  no speech heard (game sound / music / noise), skipping")
            return None
        clipbot.build_video(src, out, CREDIT, srt=ass if has_captions else None)
        title, desc = live_text(stream_title, clipbot.LAST_TRANSCRIPT)
        if clipbot.DRY_RUN:
            keep = Path(f"dryrun_{clip_id}.mp4")
            keep.write_bytes(out.read_bytes())
            log(f"  dry run saved {keep} | title: {title}")
            return None
        message_id, file_id = clipbot.send_video(out, title, desc, clip_id)
        log("  sent to Telegram")
    return {"id": clip_id, "title": title, "description": desc, "message_id": message_id,
            "file_id": file_id, "sent_at": int(time.time()), "status": "pending"}


# --------------------------------------------------------- saving to GitHub
def git(*args, check=True):
    return subprocess.run(["git", *args], capture_output=True, text=True, check=check)


def save_item(item):
    """Add one new item to queue.json on GitHub, safe even if the publisher changed the
    file at the same moment (we always start from the newest copy)."""
    if not os.environ.get("GITHUB_ACTIONS"):
        q = clipbot.load_queue()
        q["items"].append(item)
        clipbot.save_queue(q)
        return True
    branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main"
    git("config", "user.name", "clip-robot")
    git("config", "user.email", "clip-robot@users.noreply.github.com")
    for attempt in range(5):
        try:
            git("fetch", "origin", branch)
            git("reset", "--hard", f"origin/{branch}")
            q = clipbot.load_queue()
            if all(i["id"] != item["id"] for i in q["items"]):
                q["items"].append(item)
            clipbot.save_queue(q)
            git("add", "queue.json")
            git("commit", "-m", "live clip queued")
            git("push", "origin", f"HEAD:{branch}")
            return True
        except subprocess.CalledProcessError as e:
            log(f"  save attempt {attempt + 1} failed: {(e.stderr or '')[:200]}")
            time.sleep(3 + attempt * 3)
    return False


def start_publisher():
    """Ask GitHub to run the publisher now (so we do not wait for its 5-hourly timer)."""
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    try:
        subprocess.run(["gh", "workflow", "run", "publish.yml"], check=True,
                       capture_output=True, timeout=60)
        log("publisher started")
    except Exception as e:
        log(f"could not start publisher (the scheduled one will still catch it): {e}")


# ------------------------------------------------------------------- watch
def watch(first_url, stream_title):
    seg_dir = Path(tempfile.mkdtemp(prefix="live_"))
    levels = {}
    next_idx = 0            # next piece to listen to
    restarts = 0
    rec = start_recorder(first_url, seg_dir, 0)
    started = time.time()
    trigger = None          # index of the loud moment waiting to be cut
    last_clip_at = 0.0
    clips = 0
    due = []                # times when a clip's reject window ends
    ended = False
    log(f"recording started, listening for moments (up to {MAX_WATCH_MINUTES} min)")

    def on_piece(i):
        nonlocal trigger
        levels[i] = segment_level(seg_path(seg_dir, i))
        hit, now, normal = is_moment(levels, i)
        if i % 10 == 0:
            log(f"  piece {i}: {levels[i]:.1f} dB")
        if hit and trigger is None:
            if clips >= MAX_LIVE_CLIPS:
                return
            if time.time() - last_clip_at < COOLDOWN_SECONDS:
                log(f"  loud moment at piece {i} ({now:.1f} dB vs normal {normal:.1f}) "
                    f"but cooling down")
                return
            trigger = i
            log(f"  LOUD MOMENT at piece {i}: {now:.1f} dB vs normal {normal:.1f} dB")

    def do_cut(upto):
        nonlocal trigger, last_clip_at, clips
        first = max(0, trigger - PRE_SEGS)
        last = min(upto, trigger + POST_SEGS)
        trigger = None
        last_clip_at = time.time()
        try:
            item = make_clip(seg_dir, first, last, stream_title)
        except Exception as e:
            log(f"  clip failed: {type(e).__name__}: {e}")
            clipbot.notify(f"Live clip failed: {type(e).__name__}")
            return
        if item:
            clips += 1
            if save_item(item):
                due.append(time.time() + (WINDOW_MINUTES + 1) * 60)
            else:
                clipbot.notify("A live clip was sent but could not be saved to GitHub, "
                               "so it will NOT be posted to YouTube.")

    while True:
        recording_over = rec.poll() is not None

        # listen to every finished piece
        while seg_path(seg_dir, next_idx).exists() and (
                seg_path(seg_dir, next_idx + 1).exists() or recording_over):
            on_piece(next_idx)
            next_idx += 1
            if trigger is not None and next_idx - 1 >= trigger + POST_SEGS:
                do_cut(next_idx - 1)
            old = next_idx - KEEP_SEGS
            if old >= 0:
                seg_path(seg_dir, old).unlink(missing_ok=True)

        if recording_over:
            if trigger is not None:  # stream ended right after a moment: cut what we have
                do_cut(next_idx - 1)
            err = (rec.stderr.read() or b"").decode(errors="ignore")[-300:] if rec.stderr else ""
            if err:
                log(f"  recorder said: {err.strip()}")
            if SOURCE_URL or restarts >= 5 or time.time() - started > MAX_WATCH_MINUTES * 60:
                break
            time.sleep(45)  # maybe a hiccup: is he still live?
            try:
                live, url, _ = kick_live()
            except Exception:
                live, url = True, ""
            if not live:
                log("stream ended")
                break
            if url:
                restarts += 1
                log(f"stream still live, restarting recorder ({restarts})")
                rec = start_recorder(url, seg_dir, next_idx)
            continue

        if time.time() - started > MAX_WATCH_MINUTES * 60:
            log("time limit reached, stopping (the next scheduled run continues)")
            rec.terminate()
            break

        if due and time.time() >= due[0]:
            due.pop(0)
            start_publisher()
        time.sleep(2)

    # let the last clips finish their reject window, then ask for publishing
    for t in due:
        wait = t - time.time()
        if wait > 0:
            log(f"waiting {wait / 60:.0f} min for a clip's reject window to end")
            time.sleep(wait)
        start_publisher()
    try:
        rec.terminate()
    except Exception:
        pass
    return clips


# ------------------------------------------------------------------- main
def main():
    if "--check-live" in sys.argv:
        return check_live_mode()

    if not clipbot.DRY_RUN and not (clipbot.TG_TOKEN and clipbot.TG_CHAT):
        log("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing")
        return 1

    if SOURCE_URL:
        url, title = SOURCE_URL, "test stream"
    else:
        try:
            live, url, title = kick_live()
        except Exception as e:
            log(f"could not read Kick: {type(e).__name__}: {e}")
            clipbot.notify(f"Live catcher could not read Kick for '{CHANNEL}': {e}")
            return 1
        if not live:
            log(f"{CHANNEL} is offline, nothing to do")
            return 0
        if not url:
            clipbot.notify(f"{CREDIT} is live but Kick gave no stream link, so the live "
                           f"catcher could not start.")
            return 1
        clipbot.notify(f"{CREDIT} is live. The live catcher is listening for big moments.")

    n = watch(url, title)
    log(f"done, {n} live clip(s) sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
