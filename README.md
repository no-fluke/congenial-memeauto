# AppX Course Downloader — Telegram Bot

[![Deploy to Heroku](https://www.herokucdn.com/deploy/button.svg)](https://heroku.com/deploy?template=https://github.com/no-fluke/congenial-memeauto).

Converts the Termux v3.8 AppX/Parmar Academy downloader into a full Telegram bot, 
built on the Saini Bot template.

---

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

Also install system tools (required for video download):
```bash
# Ubuntu/Debian
apt install ffmpeg aria2

# or via pip
pip install yt-dlp
```

### 2. Configure environment variables

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
nano .env
```

**Required:**
| Variable | Description |
|----------|-------------|
| `API_ID` | From https://my.telegram.org |
| `API_HASH` | From https://my.telegram.org |
| `BOT_TOKEN` | From @BotFather |
| `OWNER` | Your Telegram user ID |
| `MONGO_URL` | MongoDB connection string |

**Optional:**
| Variable | Description |
|----------|-------------|
| `CREDIT` | Name shown in video captions |
| `VIMEO_WORKER_URL` | For Vimeo link support |
| `cookies_file_path` | YouTube cookies path |

### 3. Run the bot
```bash
python main.py
```

---

## Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome screen |
| `/appx` | **Start AppX Course Downloader** ← main command |
| `/stop` | Cancel running download |
| `/plan` | Check your subscription status |
| `/id` | Get your Telegram user ID |
| `/info` | Your Telegram info |
| `/logs` | View bot activity logs |
| `/cookies` | Upload YouTube cookies (.txt) |
| `/getcookies` | Download current cookies file |
| `/mfile` | Download main.py |

**Owner only:**
| Command | Description |
|---------|-------------|
| `/addauth <user_id> <days>` | Add premium user |
| `/rmauth <user_id>` | Remove user |
| `/users` | List all premium users |
| `/reset` | Restart bot |
| `/broadcast` | Broadcast to all users |
| `/broadusers` | List broadcast users |

---

## How /appx Works

The bot replicates the exact Termux flow, but over Telegram chat:

1. **Enter API domain** — e.g. `parmaracademyapi.teachx.in`
2. **Login** — Email/password or JWT token
3. **Select course(s)** from your purchased list
4. **Choose download type** — Videos / PDFs / Everything / Quizzes / Test Series
5. **Select quality** — 144p to 1080p
6. **Pick subjects & topics** — multi-select with range support (`1-5`, `all`)
7. **Set start lecture** — resume from any point
8. **Choose upload destination** — current chat or a channel ID
9. **Optional watermark** — adds scrolling text overlay on videos
10. Bot downloads each lecture and sends it directly to Telegram

### Supported content types
- 🎥 HLS streams (AES-128 encrypted + plain)
- 🎥 Direct MP4 from AppX CDN (AES decrypted)
- 🎥 XOR-encrypted MKV files
- 🎥 YouTube videos
- 📕 Encrypted PDFs (all 3 encoding variants)
- 📝 Quizzes (saved as .txt)
- 🧪 Test Series (Monthly Blitz, Weekly Test, etc.)

---

## Deploy on Railway / Render / Heroku

Set all environment variables in the platform dashboard.
The bot runs as a single `python main.py` process — no web server needed.

For Heroku, a `Procfile` is included in the original template.

---

## Files

| File | Purpose |
|------|---------|
| `main.py` | **Main bot** — all handlers + AppX downloader logic |
| `saini.py` | Video download/watermark helpers |
| `utils.py` | Progress bar |
| `db.py` | MongoDB auth database |
| `auth.py` | Auth commands |
| `vars.py` | Config / environment variables |
| `logs.py` | Logging setup |
| `app.py` | Flask health-check endpoint |
| `DejaVuSans.ttf` | Font for watermark |
| `youtube_cookies.txt` | YouTube cookies |
