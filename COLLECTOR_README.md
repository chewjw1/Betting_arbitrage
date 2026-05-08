# Kalshi BTC Data Collector

Simple script to collect second-by-second Kalshi bid/ask data for BTC prediction markets.

## Setup (Windows PowerShell)

1. **Install Python** (if not installed):
   - Download from https://python.org
   - Check "Add to PATH" during install

2. **Download the script**:
   - Save `kalshi_collector.py` to a folder (e.g., `C:\kalshi\`)

3. **Run it**:
   ```powershell
   cd C:\kalshi
   python kalshi_collector.py
   ```

4. **Let it run** for a few hours (or overnight for best data)

5. **Stop with Ctrl+C** when done

## Output

Creates a file like `kalshi_data_20260507_143022.jsonl` containing:
```json
{"ts": 1715097622.5, "time_utc": "2026-05-07 14:30:22", "ticker": "KXBTC15M-26MAY071445-45", "target_price": 80156.87, "secs_remaining": 338, "yes_bid": 0.78, "yes_ask": 0.79, "yes_mid": 0.785, "spread": 0.01}
```

## Data Fields

| Field | Description |
|-------|-------------|
| `ts` | Unix timestamp |
| `time_utc` | Human-readable UTC time |
| `ticker` | Market identifier |
| `target_price` | BTC target price for this window |
| `secs_remaining` | Seconds until window closes |
| `yes_bid` | Best bid for YES (in dollars, 0-1) |
| `yes_ask` | Best ask for YES (in dollars, 0-1) |
| `yes_mid` | Midpoint price |
| `spread` | Bid-ask spread |

## Sharing for Analysis

After collecting data:
1. Stop the script (Ctrl+C)
2. Share the `.jsonl` file
3. We'll analyze patterns with LLM categorization

## Tips

- **More data = better patterns**: Run for 4+ hours to capture multiple windows
- **Overnight runs**: Best for capturing different time-of-day patterns
- **File size**: ~50KB per hour of collection
