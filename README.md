# Peller Kick clip robot

## What it does

**Clip robot** (`clips.yml`, every 5 hours, up to 2 clips per run)
1. reads Peller's recent Kick clips and picks the 2 most-viewed new ones
2. makes a 9:16 video with his name on top and captions at the bottom
3. sends it to your Telegram with a **Reject** button

**Publisher** (`publish.yml`, 35 and 55 minutes after each clip run)
1. checks Telegram for Reject taps
2. rejected clips are dropped
3. clips older than 30 minutes that you did not reject are uploaded to YouTube
4. you get a Telegram message with the link

## Secrets (Settings > Secrets and variables > Actions)

| Secret | What |
|---|---|
| `TELEGRAM_BOT_TOKEN` | token of a bot used ONLY by this robot (see below) |
| `TELEGRAM_CHAT_ID` | your chat ID (same number as before) |
| `ANTHROPIC_API_KEY` | optional, better titles |
| `YT_CLIENT_ID`, `YT_CLIENT_SECRET`, `YT_REFRESH_TOKEN` | YouTube upload. Get them with `get_youtube_token.py` |

### Use a separate Telegram bot

The publisher reads your Reject taps from the bot. If another robot (like Ubuntu News)
reads from the same bot, they steal each other's taps. Make a new bot with @BotFather,
put its token in `TELEGRAM_BOT_TOKEN`, then open the new bot in Telegram and press **Start**.

### YouTube

1. Create a Google Cloud project, turn on **YouTube Data API v3**, make an OAuth
   "Desktop app" client, and download it as `client_secret.json`.
2. On your computer: `pip install google-auth-oauthlib`, then
   `python get_youtube_token.py client_secret.json`. Sign in with the Google account
   that owns your channel. Copy the three printed values into the `YT_` secrets.

Until the `YT_` secrets exist, clips are sent to Telegram but never posted.

## Limits to know about

- **Private uploads:** new Google Cloud projects that have not passed Google's API audit
  can only upload videos as private. If your uploads come out private, you can switch them
  to public by hand in YouTube Studio, or apply for the audit.
- **Daily quota:** the default YouTube quota allows about 6 uploads a day. The robot
  makes up to 10 clips a day, so it stops at `MAX_UPLOADS_PER_DAY` (6) and tells you
  which clips it skipped.
- **Time:** 5-hour runs happen at 00, 05, 10, 15, 20 UTC, which is 01, 06, 11, 16, 21 in Nigeria.
- **Captions** use Whisper: good for English, weaker for Pidgin, Yoruba and slang.
- Send Peller's team a message before posting, and credit him (the robot already does).
