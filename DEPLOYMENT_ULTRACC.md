# Deploying to Ultra.cc Seedbox

This guide covers deploying the prediction market arbitrage scanner to an Ultra.cc seedbox.

## Prerequisites

- Ultra.cc seedbox with SSH access
- Python 3.11+ (usually pre-installed)
- PostgreSQL (or use SQLite for simpler setup)
- Discord bot token and channel ID

## Quick Start (SQLite - Simpler)

```bash
# 1. SSH into your seedbox
ssh username@username.ultra.cc

# 2. Clone the repository
cd ~
git clone https://github.com/YOUR_USERNAME/Betting_arbitrage.git
cd Betting_arbitrage

# 3. Create virtual environment
python3 -m venv venv
source venv/bin/activate

# 4. Install dependencies
pip install --upgrade pip
pip install -r requirements.txt

# 5. Install Playwright browsers (for scraping)
playwright install chromium

# 6. Create .env file
cp .env.example .env
nano .env  # Edit with your settings
```

## Environment Configuration (.env)

```bash
# Database - Use SQLite for simplicity on seedbox
DATABASE_URL=sqlite+aiosqlite:///./arbitrage.db
DATABASE_URL_SYNC=sqlite:///./arbitrage.db

# Skip Redis if not available (will use in-memory)
# REDIS_URL=redis://localhost:6379/0

# Kalshi API (REQUIRED for full functionality)
KALSHI_API_KEY=your_kalshi_api_key
KALSHI_PRIVATE_KEY_PATH=./kalshi_private_key.pem

# Polymarket (read-only, no auth needed)
POLYMARKET_API_HOST=https://clob.polymarket.com

# Discord Notifications (REQUIRED)
DISCORD_BOT_TOKEN=your_discord_bot_token
DISCORD_CHANNEL_ID=your_channel_id
DISCORD_GUILD_ID=your_server_id

# Arbitrage Settings
MIN_NET_SPREAD_PCT=3.0          # 3% minimum net profit to alert
MAX_POSITION_SIZE=500           # $500 per side

# Polling Intervals
API_POLL_INTERVAL_SECONDS=60    # API platforms every 1 min
SCRAPE_POLL_INTERVAL_SECONDS=180  # Scraping every 3 min

# Notification deduplication
NOTIFICATION_COOLDOWN_SECONDS=300  # 5 min cooldown

# Dashboard
API_HOST=0.0.0.0
API_PORT=44495  # Custom port for ultra.cc

# Logging
LOG_LEVEL=INFO
```

## Setting Up Kalshi API Key

1. Log into Kalshi and go to Settings > API
2. Generate a new API key
3. Download the private key PEM file
4. Upload to seedbox:
   ```bash
   scp kalshi_private_key.pem username@username.ultra.cc:~/Betting_arbitrage/
   ```

## Setting Up Discord Bot

1. Go to https://discord.com/developers/applications
2. Create New Application → Name it "Arbitrage Bot"
3. Go to Bot → Add Bot → Copy Token
4. Enable these Intents:
   - Message Content Intent
   - Server Members Intent
5. Go to OAuth2 → URL Generator:
   - Scopes: `bot`, `applications.commands`
   - Permissions: `Send Messages`, `Add Reactions`, `Read Message History`
6. Use generated URL to invite bot to your server
7. Get Channel ID: Right-click channel → Copy ID (enable Developer Mode in Discord settings)

## Running the Scanner

### Option 1: Screen Session (Simple)

```bash
# Start a screen session
screen -S arbitrage

# Activate environment and run
cd ~/Betting_arbitrage
source venv/bin/activate
python -m src.scheduler.jobs

# Detach: Ctrl+A, then D
# Reattach: screen -r arbitrage
```

### Option 2: Systemd Service (Recommended)

Create `~/.config/systemd/user/arbitrage.service`:

```ini
[Unit]
Description=Prediction Market Arbitrage Scanner
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/USERNAME/Betting_arbitrage
Environment="PATH=/home/USERNAME/Betting_arbitrage/venv/bin"
ExecStart=/home/USERNAME/Betting_arbitrage/venv/bin/python -m src.scheduler.jobs
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
```

Then:
```bash
# Enable and start
systemctl --user daemon-reload
systemctl --user enable arbitrage
systemctl --user start arbitrage

# Check status
systemctl --user status arbitrage

# View logs
journalctl --user -u arbitrage -f
```

### Option 3: PM2 (if available)

```bash
# Install PM2 if not present
npm install -g pm2

# Start the scanner
cd ~/Betting_arbitrage
pm2 start "source venv/bin/activate && python -m src.scheduler.jobs" --name arbitrage

# Save and set to restart on reboot
pm2 save
pm2 startup
```

## Running the Dashboard

```bash
# In a separate screen/service
source venv/bin/activate
uvicorn src.api.main:app --host 0.0.0.0 --port 44495
```

Access at: `http://username.ultra.cc:44495/docs`

## SQLite Setup (instead of PostgreSQL)

Add to requirements.txt:
```
aiosqlite>=0.19.0
```

Update `src/database/session.py` to handle SQLite:
```python
# The existing code should work with SQLite URL
# Just ensure aiosqlite is installed
```

## Monitoring

### Check if running:
```bash
ps aux | grep python
```

### View logs:
```bash
# If using screen
screen -r arbitrage

# If using systemd
journalctl --user -u arbitrage -f --no-pager -n 100
```

### Test Discord connection:
```bash
cd ~/Betting_arbitrage
source venv/bin/activate
python -c "
from src.config import get_settings
s = get_settings()
print(f'Discord Token: {s.discord_bot_token[:10]}...')
print(f'Channel ID: {s.discord_channel_id}')
"
```

## Playwright Setup for Scraping (DraftKings, FanDuel, IBKR)

The scrapers require Playwright with Chromium. These platforms don't need login - they scrape public prices.

### Option 1: Direct Install (if you have sudo access)

```bash
# Install system dependencies
sudo apt-get update
sudo apt-get install -y \
  libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
  libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
  libxdamage1 libxfixes3 libxrandr2 libgbm1 \
  libpango-1.0-0 libcairo2 libasound2

# Install Playwright and browsers
pip install playwright
playwright install chromium

# Test it works
python scripts/test_scraping.py
```

### Option 2: Docker Container

```bash
# Use the official Playwright Python image
docker run -it --rm \
  -v ~/Betting_arbitrage:/app \
  -w /app \
  mcr.microsoft.com/playwright/python:v1.40.0-focal \
  python -m src.scheduler.jobs
```

Or add a `docker-compose.yml`:
```yaml
version: '3.8'
services:
  scanner:
    image: mcr.microsoft.com/playwright/python:v1.40.0-focal
    volumes:
      - .:/app
    working_dir: /app
    command: python -m src.scheduler.jobs
    env_file: .env
    restart: unless-stopped
```

### Option 3: API-Only Mode (No Scraping)

If Playwright doesn't work, run in API-only mode:

```python
# In src/scheduler/jobs.py, comment out scraping collectors:
# collectors = {
#     "draftkings": DraftKingsCollector(),
#     "fanduel": FanDuelCollector(),
#     "ibkr": IBKRCollector(),
# }
```

This still gives you:
- PredictIt (756+ markets)
- Polymarket (400+ markets)
- Kalshi (70+ markets)

## Troubleshooting

### SSL/TLS errors:
```bash
# Update certificates
pip install --upgrade certifi

# Or in code, the collectors already handle this with verify=False
```

### Memory issues:
```bash
# Check memory usage
free -h

# If low, disable scraping collectors (API only)
# Set SCRAPE_POLL_INTERVAL_SECONDS=0 or very high
```

### Port already in use:
```bash
# Find what's using the port
lsof -i :44495

# Use a different port in .env if needed
API_PORT=44496
```

## Quick Health Check Script

Save as `~/Betting_arbitrage/scripts/health_check.sh`:

```bash
#!/bin/bash
cd ~/Betting_arbitrage
source venv/bin/activate

python -c "
import asyncio
from src.collectors import PredictItCollector

async def check():
    async with PredictItCollector() as collector:
        markets = await collector.fetch_markets()
        print(f'✓ PredictIt: {len(markets)} markets')

asyncio.run(check())
"
```

Run: `bash scripts/health_check.sh`
