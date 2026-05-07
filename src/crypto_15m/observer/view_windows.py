#!/usr/bin/env python3
"""View and analyze collected windows."""

import json
from pathlib import Path
from datetime import datetime

DATA_DIR = Path(__file__).parent.parent.parent.parent / "data" / "windows"


def view_windows():
    """Display summary of collected windows."""
    windows = list(DATA_DIR.glob("*.json"))

    if not windows:
        print("No windows collected yet.")
        print(f"Data directory: {DATA_DIR}")
        return

    print("=" * 80)
    print("COLLECTED WINDOWS SUMMARY")
    print("=" * 80)
    print(f"\nTotal windows: {len(windows)}")
    print(f"Data directory: {DATA_DIR}\n")

    # Load and display each window
    all_data = []
    for wf in sorted(windows):
        with open(wf) as f:
            data = json.load(f)
            all_data.append(data)

    # Summary table
    print(f"{'Window':<20} {'Outcome':>8} {'Range%':>8} {'Ticks':>6} {'Category':<15} {'Predictability':<15}")
    print("-" * 80)

    for d in all_data:
        window_id = d['window_id'][-18:]
        outcome = d.get('outcome', '?')
        range_pct = d.get('btc_range_pct', 0)
        ticks = d.get('tick_count', len(d.get('ticks', [])))
        category = d.get('llm_category', 'no analysis')[:14]
        predictability = d.get('llm_predictability', '-')[:14]

        print(f"{window_id:<20} {outcome:>8} {range_pct:>7.3f}% {ticks:>6} {category:<15} {predictability:<15}")

    # Statistics
    print("\n" + "=" * 80)
    print("STATISTICS")
    print("=" * 80)

    outcomes = [d.get('outcome') for d in all_data if d.get('outcome')]
    if outcomes:
        ups = outcomes.count('UP')
        downs = outcomes.count('DOWN')
        print(f"\nOutcomes: {ups} UP, {downs} DOWN ({ups/len(outcomes)*100:.1f}% UP)")

    # Category distribution
    categories = [d.get('llm_category') for d in all_data if d.get('llm_category')]
    if categories:
        print("\nCategory distribution:")
        from collections import Counter
        for cat, count in Counter(categories).most_common():
            print(f"  {cat}: {count} ({count/len(categories)*100:.0f}%)")

    # Predictability analysis
    predictabilities = [d.get('llm_predictability') for d in all_data if d.get('llm_predictability')]
    if predictabilities:
        print("\nPredictability:")
        from collections import Counter
        for pred, count in Counter(predictabilities).most_common():
            print(f"  {pred}: {count} ({count/len(predictabilities)*100:.0f}%)")

    # LLM summaries
    summaries = [(d['window_id'][-12:], d.get('llm_summary')) for d in all_data if d.get('llm_summary')]
    if summaries:
        print("\n" + "=" * 80)
        print("LLM SUMMARIES")
        print("=" * 80)
        for window_id, summary in summaries[-5:]:
            print(f"\n{window_id}:")
            print(f"  {summary}")


def view_window_detail(window_id: str):
    """View detailed data for a specific window."""
    # Find the window file
    matches = list(DATA_DIR.glob(f"*{window_id}*.json"))
    if not matches:
        print(f"Window not found: {window_id}")
        return

    with open(matches[0]) as f:
        data = json.load(f)

    print("=" * 80)
    print(f"WINDOW: {data['window_id']}")
    print("=" * 80)

    print(f"\nTarget: ${data['target_price']:,.2f}")
    print(f"Outcome: {data.get('outcome', 'unknown')}")
    print(f"Open: ${data.get('open_btc', 0):,.2f}")
    print(f"Close: ${data.get('close_btc', 0):,.2f}")
    print(f"High: ${data.get('high_btc', 0):,.2f}")
    print(f"Low: ${data.get('low_btc', 0):,.2f}")
    print(f"Range: {data.get('btc_range_pct', 0):.3f}%")
    print(f"Ticks: {len(data.get('ticks', []))}")

    if data.get('llm_category'):
        print(f"\nLLM Analysis:")
        print(f"  Category: {data['llm_category']}")
        print(f"  Predictability: {data.get('llm_predictability', '-')}")
        print(f"  Patterns: {', '.join(data.get('llm_patterns', []))}")
        print(f"  Summary: {data.get('llm_summary', '-')}")

    # Show tick samples
    ticks = data.get('ticks', [])
    if ticks:
        print(f"\nTick samples (every 60s):")
        for i, tick in enumerate(ticks):
            if i % 60 == 0 or i == len(ticks) - 1:
                print(
                    f"  T-{tick['secs_remaining']:3}s | "
                    f"BTC: ${tick['btc_price']:,.0f} | "
                    f"Dist: {tick['distance_pct']:+.3f}% | "
                    f"YES: {tick['yes_mid']*100:.0f}c"
                )


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        view_window_detail(sys.argv[1])
    else:
        view_windows()
