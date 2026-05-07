#!/usr/bin/env python3
"""Analyze collected pattern data to find market mispricing."""

import sqlite3
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent.parent.parent / "data"
DB_PATH = DATA_DIR / "pattern_data.db"


def analyze():
    """Analyze collected data for mispricing patterns."""
    if not DB_PATH.exists():
        print("No data collected yet. Run pattern_collector.py first.")
        return

    db = sqlite3.connect(DB_PATH)

    print("=" * 70)
    print("MARKET MISPRICING ANALYSIS")
    print("=" * 70)

    # Total windows
    cursor = db.execute("SELECT COUNT(*) FROM windows WHERE outcome IS NOT NULL")
    total = cursor.fetchone()[0]
    print(f"\nTotal windows analyzed: {total}")

    if total < 5:
        print("Need more data for meaningful analysis. Keep collector running.")
        db.close()
        return

    # Overall stats
    cursor = db.execute("""
        SELECT
            AVG(CASE WHEN outcome = 'UP' THEN 1.0 ELSE 0.0 END) * 100,
            AVG(market_was_right) * 100
        FROM windows WHERE outcome IS NOT NULL
    """)
    up_pct, accuracy = cursor.fetchone()
    print(f"Overall UP rate: {up_pct:.1f}%")
    print(f"Market accuracy: {accuracy:.1f}%")

    # By hour - find exploitable patterns
    print("\n" + "=" * 70)
    print("BY HOUR (UTC) - Looking for mispricing")
    print("=" * 70)
    cursor = db.execute("""
        SELECT utc_hour,
               COUNT(*) as n,
               AVG(CASE WHEN outcome = 'UP' THEN 1.0 ELSE 0.0 END) * 100 as actual_up_pct,
               AVG(open_yes_mid) * 100 as market_implied_pct
        FROM windows
        WHERE outcome IS NOT NULL
        GROUP BY utc_hour
        ORDER BY utc_hour
    """)

    print(f"\n{'Hour':<8} {'N':<5} {'Actual':<10} {'Market':<10} {'Edge':<10} {'Action':<15}")
    print("-" * 70)

    edges_found = []
    for row in cursor.fetchall():
        hour, n, actual, market = row
        edge = actual - market

        if edge > 10 and n >= 3:
            action = "BET YES ***"
            edges_found.append(("hour", hour, edge, "YES"))
        elif edge < -10 and n >= 3:
            action = "BET NO ***"
            edges_found.append(("hour", hour, edge, "NO"))
        else:
            action = "-"

        print(f"{hour:02d}:00    {n:<5} {actual:>8.1f}%  {market:>8.1f}%  {edge:>+8.1f}%  {action}")

    # By momentum
    print("\n" + "=" * 70)
    print("BY MOMENTUM STATE - Looking for continuation/reversal patterns")
    print("=" * 70)
    cursor = db.execute("""
        SELECT momentum_state,
               COUNT(*) as n,
               AVG(CASE WHEN outcome = 'UP' THEN 1.0 ELSE 0.0 END) * 100 as actual_up_pct,
               AVG(open_yes_mid) * 100 as market_implied_pct
        FROM windows
        WHERE outcome IS NOT NULL AND momentum_state != 'insufficient_data'
        GROUP BY momentum_state
        HAVING n >= 2
        ORDER BY (AVG(CASE WHEN outcome = 'UP' THEN 1.0 ELSE 0.0 END) - AVG(open_yes_mid)) DESC
    """)

    print(f"\n{'Momentum':<20} {'N':<5} {'Actual':<10} {'Market':<10} {'Edge':<10} {'Action':<15}")
    print("-" * 70)

    for row in cursor.fetchall():
        momentum, n, actual, market = row
        edge = actual - market

        if edge > 10 and n >= 2:
            action = "BET YES ***"
            edges_found.append(("momentum", momentum, edge, "YES"))
        elif edge < -10 and n >= 2:
            action = "BET NO ***"
            edges_found.append(("momentum", momentum, edge, "NO"))
        else:
            action = "-"

        print(f"{momentum:<20} {n:<5} {actual:>8.1f}%  {market:>8.1f}%  {edge:>+8.1f}%  {action}")

    # By distance from target
    print("\n" + "=" * 70)
    print("BY OPENING DISTANCE FROM TARGET - Does bigger gap = more predictable?")
    print("=" * 70)
    cursor = db.execute("""
        SELECT
            CASE
                WHEN ABS(open_distance_pct) < 0.05 THEN 'Very close (<0.05%)'
                WHEN ABS(open_distance_pct) < 0.1 THEN 'Close (0.05-0.1%)'
                WHEN ABS(open_distance_pct) < 0.15 THEN 'Medium (0.1-0.15%)'
                ELSE 'Far (>0.15%)'
            END as distance_bucket,
            COUNT(*) as n,
            AVG(market_was_right) * 100 as market_accuracy,
            AVG(ABS(open_distance_pct)) as avg_distance
        FROM windows
        WHERE outcome IS NOT NULL
        GROUP BY distance_bucket
        ORDER BY avg_distance
    """)

    print(f"\n{'Distance':<25} {'N':<5} {'Market Accuracy':<18} {'Avg Distance':<12}")
    print("-" * 70)

    for row in cursor.fetchall():
        bucket, n, accuracy, avg_dist = row
        print(f"{bucket:<25} {n:<5} {accuracy:>14.1f}%    {avg_dist:>10.3f}%")

    # Summary
    print("\n" + "=" * 70)
    print("ACTIONABLE EDGES FOUND")
    print("=" * 70)

    if edges_found:
        print("\nPotential systematic bets:")
        for edge_type, condition, edge, direction in edges_found:
            print(f"  - When {edge_type} = {condition}: Bet {direction} (edge: {edge:+.1f}%)")

        print("\nTo exploit:")
        print("  1. Wait for condition to occur")
        print("  2. Place bet at market open")
        print("  3. Let it ride to settlement")
        print("  4. Track results, adjust if edge disappears")
    else:
        print("\nNo significant edges found yet.")
        print("Keep collecting data - need 50+ windows per condition for confidence.")

    # Recent windows
    print("\n" + "=" * 70)
    print("RECENT WINDOWS")
    print("=" * 70)
    cursor = db.execute("""
        SELECT window_id, utc_hour, momentum_state, open_yes_mid, outcome, market_was_right
        FROM windows
        WHERE outcome IS NOT NULL
        ORDER BY id DESC
        LIMIT 10
    """)

    print(f"\n{'Window':<30} {'Hour':<6} {'Momentum':<15} {'Open':<8} {'Result':<8} {'Market':<8}")
    print("-" * 80)
    for row in cursor.fetchall():
        wid, hour, momentum, open_yes, outcome, was_right = row
        wid_short = wid[-20:] if wid else ""
        print(f"{wid_short:<30} {hour:02d}:00  {momentum or '-':<15} {open_yes*100:>5.1f}c  {outcome:<8} {'✓' if was_right else '✗'}")

    db.close()


if __name__ == "__main__":
    analyze()
