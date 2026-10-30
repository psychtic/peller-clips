# Peller Kick clip robot (Stage 1)

Every 3 hours, on GitHub's free servers, this robot:

1. reads Peller's recent clips from Kick and picks the 2 most-viewed new ones
2. turns each into a 9:16 vertical video with his name on top and captions at the bottom
3. writes a title and description that credit him
4. sends the finished video to your Telegram

You post it yourself for now. Stage 2 adds the 2-hour "reject window" and automatic YouTube upload.

## Setup (about 10 minutes)

1. Create a new GitHub repository (private is fine) and upload everything in this folder,
   including the hidden `.github` folder.
2. In the repo go to **Settings > Secrets and variables > Actions > New repository secret**
   and add:
   - `TELEGRAM_BOT_TOKEN` - your bot token (the same bot as Ubuntu News works)
   - `TELEGRAM_CHAT_ID` - your chat ID (the same one works)
   - `ANTHROPIC_API_KEY` - optional. With it, Claude writes better titles. Without it, the robot uses the Kick clip title.
3. Open the **Actions** tab, choose **Kick clip robot**, press **Run workflow**.

## First run: this is the real test

I could not reach Kick from where I built this, so the first run answers one question:
does Kick let GitHub's servers read clips?

- **Works:** a video arrives in your Telegram within a few minutes.
- **Blocked:** you get a Telegram message saying Kick could not be read. Tell me and I will switch to the backup route (clips from the streamer's YouTube channel or a proxy).

## Settings (edit in `.github/workflows/clips.yml`)

| Setting | Default | Meaning |
|---|---|---|
| `KICK_CHANNEL` | `peller` | the name in `kick.com/<name>`. Check this is right for Peller. |
| `CREDIT_NAME` | `Peller` | name shown on the video |
| `MAX_PER_RUN` | 2 | videos per run (add under `env:` to change) |
| `CAPTIONS` | 1 | set to 0 to skip captions |
| `DRY_RUN` | 0 | set to 1 to build videos without sending |

## Notes

- Captions use Whisper. It is good with English and weaker with Nigerian Pidgin, Yoruba and slang, so check captions before posting.
- Before you post anything, send Peller's team a short message saying you run a clips channel and will credit him in every video.
- `seen.json` is how the robot remembers which clips it already used. Do not delete it.
