#!/usr/bin/env python3
"""Generate HTML visualization of collected market data."""

import sqlite3
import json
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent.parent.parent / "data" / "continuous"
DB_PATH = DATA_DIR / "market_data.db"
OUTPUT_PATH = DATA_DIR / "dashboard.html"


def generate_dashboard():
    """Generate HTML dashboard from collected data."""

    db = sqlite3.connect(DB_PATH)

    # Get all ticks
    cursor = db.execute("""
        SELECT ts, window_id, secs_remaining, btc_price, target_price,
               distance_pct, yes_bid, yes_ask, yes_mid
        FROM ticks
        ORDER BY ts
    """)

    ticks = cursor.fetchall()

    if not ticks:
        print("No data collected yet!")
        return

    # Prepare data for charts
    timestamps = []
    btc_prices = []
    yes_mids = []
    distances = []
    secs_remaining = []
    window_ids = []

    for tick in ticks:
        ts, window_id, secs_rem, btc, target, dist, yes_bid, yes_ask, yes_mid = tick
        timestamps.append(datetime.fromtimestamp(ts).strftime('%H:%M:%S'))
        btc_prices.append(btc)
        yes_mids.append(yes_mid * 100)  # Convert to cents
        distances.append(dist)
        secs_remaining.append(secs_rem)
        window_ids.append(window_id)

    # Get window results
    cursor = db.execute("""
        SELECT window_id, target_price, outcome, open_btc, close_btc
        FROM windows
        ORDER BY end_ts DESC
    """)
    windows = cursor.fetchall()

    # Calculate correlation over rolling windows
    correlations = []
    window_size = 30  # 30-second rolling window

    for i in range(window_size, len(btc_prices)):
        btc_slice = btc_prices[i-window_size:i]
        yes_slice = yes_mids[i-window_size:i]

        # Calculate moves
        btc_moves = [btc_slice[j] - btc_slice[j-1] for j in range(1, len(btc_slice))]
        yes_moves = [yes_slice[j] - yes_slice[j-1] for j in range(1, len(yes_slice))]

        if len(btc_moves) > 0:
            # Pearson correlation
            n = len(btc_moves)
            mean_btc = sum(btc_moves) / n
            mean_yes = sum(yes_moves) / n

            cov = sum((btc_moves[j] - mean_btc) * (yes_moves[j] - mean_yes) for j in range(n)) / n
            std_btc = (sum((x - mean_btc)**2 for x in btc_moves) / n) ** 0.5
            std_yes = (sum((x - mean_yes)**2 for x in yes_moves) / n) ** 0.5

            if std_btc > 0 and std_yes > 0:
                corr = cov / (std_btc * std_yes)
            else:
                corr = 0
            correlations.append(corr)
        else:
            correlations.append(0)

    # Pad correlations for alignment
    correlations = [0] * window_size + correlations

    # Stats
    stats = {
        "total_ticks": len(ticks),
        "windows_completed": len(windows),
        "btc_range": f"${min(btc_prices):,.0f} - ${max(btc_prices):,.0f}",
        "yes_range": f"{min(yes_mids):.0f}c - {max(yes_mids):.0f}c",
        "avg_correlation": sum(correlations) / len(correlations) if correlations else 0,
    }

    # Window outcomes
    window_results = []
    for w in windows:
        window_results.append({
            "id": w[0],
            "target": w[1],
            "outcome": w[2].upper() if w[2] else "PENDING",
            "open": w[3],
            "close": w[4],
        })

    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>BTC/Kalshi Market Data Dashboard</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #1a1a2e;
            color: #eee;
            margin: 0;
            padding: 20px;
        }}
        .header {{
            text-align: center;
            margin-bottom: 30px;
        }}
        .header h1 {{
            color: #00d4ff;
            margin-bottom: 5px;
        }}
        .header p {{
            color: #888;
        }}
        .stats {{
            display: flex;
            justify-content: center;
            gap: 30px;
            margin-bottom: 30px;
            flex-wrap: wrap;
        }}
        .stat-box {{
            background: #16213e;
            padding: 15px 25px;
            border-radius: 10px;
            text-align: center;
        }}
        .stat-value {{
            font-size: 24px;
            font-weight: bold;
            color: #00d4ff;
        }}
        .stat-label {{
            font-size: 12px;
            color: #888;
            margin-top: 5px;
        }}
        .charts {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
            margin-bottom: 30px;
        }}
        .chart-container {{
            background: #16213e;
            padding: 20px;
            border-radius: 10px;
        }}
        .chart-container.full {{
            grid-column: span 2;
        }}
        .chart-title {{
            color: #00d4ff;
            margin-bottom: 15px;
            font-size: 16px;
        }}
        canvas {{
            max-height: 300px;
        }}
        .windows {{
            background: #16213e;
            padding: 20px;
            border-radius: 10px;
        }}
        .windows h3 {{
            color: #00d4ff;
            margin-top: 0;
        }}
        .window-row {{
            display: flex;
            justify-content: space-between;
            padding: 10px;
            border-bottom: 1px solid #333;
        }}
        .outcome-up {{
            color: #00ff88;
        }}
        .outcome-down {{
            color: #ff4466;
        }}
        @media (max-width: 800px) {{
            .charts {{
                grid-template-columns: 1fr;
            }}
            .chart-container.full {{
                grid-column: span 1;
            }}
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>BTC/Kalshi 15M Market Dashboard</h1>
        <p>Real-time correlation analysis | Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
    </div>

    <div class="stats">
        <div class="stat-box">
            <div class="stat-value">{stats['total_ticks']}</div>
            <div class="stat-label">Data Points</div>
        </div>
        <div class="stat-box">
            <div class="stat-value">{stats['windows_completed']}</div>
            <div class="stat-label">Windows Completed</div>
        </div>
        <div class="stat-box">
            <div class="stat-value">{stats['btc_range']}</div>
            <div class="stat-label">BTC Range</div>
        </div>
        <div class="stat-box">
            <div class="stat-value">{stats['yes_range']}</div>
            <div class="stat-label">YES Price Range</div>
        </div>
        <div class="stat-box">
            <div class="stat-value">{stats['avg_correlation']:.2f}</div>
            <div class="stat-label">Avg Correlation</div>
        </div>
    </div>

    <div class="charts">
        <div class="chart-container full">
            <div class="chart-title">BTC Price vs Kalshi YES Price</div>
            <canvas id="priceChart"></canvas>
        </div>

        <div class="chart-container">
            <div class="chart-title">Distance from Target (%)</div>
            <canvas id="distanceChart"></canvas>
        </div>

        <div class="chart-container">
            <div class="chart-title">Rolling Correlation (30s window)</div>
            <canvas id="corrChart"></canvas>
        </div>
    </div>

    <div class="windows">
        <h3>Completed Windows</h3>
        {"".join(f'''
        <div class="window-row">
            <span>{w["id"][-15:]}</span>
            <span>Target: ${w["target"]:,.2f}</span>
            <span class="outcome-{"up" if w["outcome"] == "UP" else "down"}">{w["outcome"]}</span>
        </div>
        ''' for w in window_results[:10]) if window_results else '<p>No completed windows yet</p>'}
    </div>

    <script>
        const timestamps = {json.dumps(timestamps)};
        const btcPrices = {json.dumps(btc_prices)};
        const yesMids = {json.dumps(yes_mids)};
        const distances = {json.dumps(distances)};
        const correlations = {json.dumps(correlations)};

        // Downsample for performance if needed
        const maxPoints = 500;
        const step = Math.max(1, Math.floor(timestamps.length / maxPoints));

        const sampledTimestamps = timestamps.filter((_, i) => i % step === 0);
        const sampledBtc = btcPrices.filter((_, i) => i % step === 0);
        const sampledYes = yesMids.filter((_, i) => i % step === 0);
        const sampledDist = distances.filter((_, i) => i % step === 0);
        const sampledCorr = correlations.filter((_, i) => i % step === 0);

        // Normalize BTC for dual axis display
        const btcMin = Math.min(...sampledBtc);
        const btcMax = Math.max(...sampledBtc);
        const yesMin = Math.min(...sampledYes);
        const yesMax = Math.max(...sampledYes);

        // Price Chart
        new Chart(document.getElementById('priceChart'), {{
            type: 'line',
            data: {{
                labels: sampledTimestamps,
                datasets: [{{
                    label: 'BTC Price ($)',
                    data: sampledBtc,
                    borderColor: '#00d4ff',
                    backgroundColor: 'rgba(0, 212, 255, 0.1)',
                    yAxisID: 'y',
                    tension: 0.1,
                    pointRadius: 0,
                }}, {{
                    label: 'YES Price (cents)',
                    data: sampledYes,
                    borderColor: '#ff9f43',
                    backgroundColor: 'rgba(255, 159, 67, 0.1)',
                    yAxisID: 'y1',
                    tension: 0.1,
                    pointRadius: 0,
                }}]
            }},
            options: {{
                responsive: true,
                interaction: {{
                    mode: 'index',
                    intersect: false,
                }},
                scales: {{
                    x: {{
                        ticks: {{ color: '#888', maxTicksLimit: 10 }},
                        grid: {{ color: '#333' }}
                    }},
                    y: {{
                        type: 'linear',
                        display: true,
                        position: 'left',
                        ticks: {{ color: '#00d4ff' }},
                        grid: {{ color: '#333' }},
                        title: {{ display: true, text: 'BTC ($)', color: '#00d4ff' }}
                    }},
                    y1: {{
                        type: 'linear',
                        display: true,
                        position: 'right',
                        ticks: {{ color: '#ff9f43' }},
                        grid: {{ drawOnChartArea: false }},
                        title: {{ display: true, text: 'YES (cents)', color: '#ff9f43' }}
                    }},
                }},
                plugins: {{
                    legend: {{ labels: {{ color: '#eee' }} }}
                }}
            }}
        }});

        // Distance Chart
        new Chart(document.getElementById('distanceChart'), {{
            type: 'line',
            data: {{
                labels: sampledTimestamps,
                datasets: [{{
                    label: 'Distance from Target (%)',
                    data: sampledDist,
                    borderColor: sampledDist.map(d => d >= 0 ? '#00ff88' : '#ff4466'),
                    segment: {{
                        borderColor: ctx => {{
                            const value = ctx.p1.parsed.y;
                            return value >= 0 ? '#00ff88' : '#ff4466';
                        }}
                    }},
                    tension: 0.1,
                    pointRadius: 0,
                    fill: true,
                    backgroundColor: 'rgba(100, 100, 100, 0.2)',
                }}]
            }},
            options: {{
                responsive: true,
                scales: {{
                    x: {{
                        ticks: {{ color: '#888', maxTicksLimit: 6 }},
                        grid: {{ color: '#333' }}
                    }},
                    y: {{
                        ticks: {{ color: '#888' }},
                        grid: {{ color: '#333' }}
                    }}
                }},
                plugins: {{
                    legend: {{ display: false }},
                    annotation: {{
                        annotations: {{
                            line1: {{
                                type: 'line',
                                yMin: 0,
                                yMax: 0,
                                borderColor: '#666',
                                borderWidth: 1,
                                borderDash: [5, 5],
                            }}
                        }}
                    }}
                }}
            }}
        }});

        // Correlation Chart
        new Chart(document.getElementById('corrChart'), {{
            type: 'line',
            data: {{
                labels: sampledTimestamps,
                datasets: [{{
                    label: 'Correlation',
                    data: sampledCorr,
                    borderColor: '#a855f7',
                    backgroundColor: 'rgba(168, 85, 247, 0.2)',
                    tension: 0.1,
                    pointRadius: 0,
                    fill: true,
                }}]
            }},
            options: {{
                responsive: true,
                scales: {{
                    x: {{
                        ticks: {{ color: '#888', maxTicksLimit: 6 }},
                        grid: {{ color: '#333' }}
                    }},
                    y: {{
                        min: -1,
                        max: 1,
                        ticks: {{ color: '#888' }},
                        grid: {{ color: '#333' }}
                    }}
                }},
                plugins: {{
                    legend: {{ display: false }}
                }}
            }}
        }});
    </script>
</body>
</html>
"""

    with open(OUTPUT_PATH, 'w') as f:
        f.write(html)

    print(f"Dashboard generated: {OUTPUT_PATH}")
    print(f"Total data points: {len(ticks)}")
    print(f"Windows completed: {len(windows)}")

    db.close()


if __name__ == "__main__":
    generate_dashboard()
