#!/usr/bin/env python3
"""
Analyze resource monitoring data from monitor_resources.py.

This script reads the CSV output from monitor_resources.py and produces
a summary report with min/max/avg values for each metric.

Usage:
    python profiling/analyze_resources.py [--input PATH] [--output PATH]

Options:
    --input PATH    Input CSV file (default: profiling/output/resources.csv)
    --output PATH   Output report path (default: profiling/output/resources_report.txt)

Examples:
    # Analyze the default CSV
    python profiling/analyze_resources.py

    # Analyze a custom CSV
    python profiling/analyze_resources.py --input /tmp/my_resources.csv
"""

import argparse
import csv
import sys
from pathlib import Path

# Project root (parent of profiling/)
PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_OUTPUT_DIR = Path(__file__).parent / "output"


def parse_args():
    parser = argparse.ArgumentParser(description="Analyze resource monitoring data")
    parser.add_argument(
        "--input",
        type=str,
        default=str(DEFAULT_OUTPUT_DIR / "resources.csv"),
        help="Input CSV file",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=str(DEFAULT_OUTPUT_DIR / "resources_report.txt"),
        help="Output report path",
    )
    return parser.parse_args()


def parse_float(value):
    """Parse a CSV value to float, or None if N/A."""
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def summarize(values):
    """Return (min, max, avg) for a list of numeric values."""
    valid = [v for v in values if v is not None]
    if not valid:
        return (None, None, None)
    return (min(valid), max(valid), sum(valid) / len(valid))


def main():
    args = parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"❌ CSV file not found: {input_path}")
        print("   Run monitor_resources.py first to generate data.")
        sys.exit(1)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Read CSV data
    rows = []
    with open(input_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    if not rows:
        print("❌ No data found in CSV file.")
        sys.exit(1)

    # Extract metrics
    metrics = {
        "app_cpu_pct": [parse_float(r["app_cpu_pct"]) for r in rows],
        "app_rss_mb": [parse_float(r["app_rss_mb"]) for r in rows],
        "app_vms_mb": [parse_float(r["app_vms_mb"]) for r in rows],
        "app_read_mb_s": [parse_float(r["app_read_mb_s"]) for r in rows],
        "app_write_mb_s": [parse_float(r["app_write_mb_s"]) for r in rows],
        "app_threads": [parse_float(r["app_threads"]) for r in rows],
        "app_fds": [parse_float(r["app_fds"]) for r in rows],
        "cli_cpu_pct": [parse_float(r["cli_cpu_pct"]) for r in rows],
        "cli_rss_mb": [parse_float(r["cli_rss_mb"]) for r in rows],
        "cli_read_mb_s": [parse_float(r["cli_read_mb_s"]) for r in rows],
        "cli_write_mb_s": [parse_float(r["cli_write_mb_s"]) for r in rows],
        "cli_threads": [parse_float(r["cli_threads"]) for r in rows],
        "cli_fds": [parse_float(r["cli_fds"]) for r in rows],
    }

    # Generate report
    with open(output_path, "w") as f:
        f.write("=" * 80 + "\n")
        f.write("  SIGNAL TUI CLIENT — RESOURCE MONITORING REPORT\n")
        f.write("=" * 80 + "\n\n")

        duration = rows[-1]["elapsed_s"] if rows else "0"
        f.write(f"  Samples:     {len(rows)}\n")
        f.write(f"  Duration:    {duration}s\n")
        if len(rows) >= 2:
            interval = float(rows[1]["elapsed_s"]) - float(rows[0]["elapsed_s"])
            f.write(f"  Interval:    ~{interval:.1f}s\n")
        f.write("\n")


        # App metrics
        f.write("-" * 80 + "\n")
        f.write("  APPLICATION (signal_tui.py)\n")
        f.write("-" * 80 + "\n")
        f.write(f"  {'Metric':<25} {'Min':>10} {'Max':>10} {'Avg':>10}\n")
        f.write(f"  {'-'*25} {'-'*10} {'-'*10} {'-'*10}\n")

        labels = {
            "app_cpu_pct": "CPU %",
            "app_rss_mb": "RSS (MB)",
            "app_vms_mb": "VMS (MB)",
            "app_read_mb_s": "Read (MB/s)",
            "app_write_mb_s": "Write (MB/s)",
            "app_threads": "Threads",
            "app_fds": "File descriptors",
        }
        for key, label in labels.items():
            mn, mx, avg = summarize(metrics[key])
            if mn is not None:
                f.write(f"  {label:<25} {mn:>10.2f} {mx:>10.2f} {avg:>10.2f}\n")
            else:
                f.write(f"  {label:<25} {'N/A':>10} {'N/A':>10} {'N/A':>10}\n")

        # Signal-cli daemon metrics
        f.write("\n" + "-" * 80 + "\n")
        f.write("  SIGNAL-CLI DAEMON\n")
        f.write("-" * 80 + "\n")
        f.write(f"  {'Metric':<25} {'Min':>10} {'Max':>10} {'Avg':>10}\n")
        f.write(f"  {'-'*25} {'-'*10} {'-'*10} {'-'*10}\n")

        cli_labels = {
            "cli_cpu_pct": "CPU %",
            "cli_rss_mb": "RSS (MB)",
            "cli_read_mb_s": "Read (MB/s)",
            "cli_write_mb_s": "Write (MB/s)",
            "cli_threads": "Threads",
            "cli_fds": "File descriptors",
        }
        for key, label in cli_labels.items():
            mn, mx, avg = summarize(metrics[key])
            if mn is not None:
                f.write(f"  {label:<25} {mn:>10.2f} {mx:>10.2f} {avg:>10.2f}\n")
            else:
                f.write(f"  {label:<25} {'N/A':>10} {'N/A':>10} {'N/A':>10}\n")

        # Peak memory
        f.write("\n" + "-" * 80 + "\n")
        f.write("  PEAK VALUES\n")
        f.write("-" * 80 + "\n")
        _, peak_rss, _ = summarize(metrics["app_rss_mb"])
        _, peak_cpu, _ = summarize(metrics["app_cpu_pct"])
        _, peak_write, _ = summarize(metrics["app_write_mb_s"])
        if peak_rss is not None:
            f.write(f"  Peak app RSS:      {peak_rss:.1f} MB\n")
        else:
            f.write(f"  Peak app RSS:      N/A\n")
        if peak_cpu is not None:
            f.write(f"  Peak app CPU:      {peak_cpu:.1f}%\n")
        else:
            f.write(f"  Peak app CPU:      N/A\n")
        if peak_write is not None:
            f.write(f"  Peak app write:    {peak_write:.3f} MB/s\n")
        else:
            f.write(f"  Peak app write:    N/A\n")

        # Memory growth over time
        rss_values = [v for v in metrics["app_rss_mb"] if v is not None]
        if len(rss_values) >= 2:
            first_rss = rss_values[0]
            last_rss = rss_values[-1]
            growth = last_rss - first_rss
            f.write(f"\n  Memory growth:     {first_rss:.1f} → {last_rss:.1f} MB ({growth:+.1f} MB)\n")
            if growth > 10:
                f.write("  ⚠️  Significant memory growth detected — possible leak!\n")
            elif growth > 0:
                f.write("  ℹ️  Some memory growth — normal for a long-running app.\n")
            else:
                f.write("  ✅ No memory growth — looks healthy.\n")
        else:
            f.write("\n  Memory growth:     N/A (insufficient RSS data)\n")


        f.write("\n" + "=" * 80 + "\n")
        f.write("  END OF REPORT\n")
        f.write("=" * 80 + "\n")

    print(f"✅ Report saved to: {output_path}")
    print()
    print("📄 To view the report:")
    print(f"   cat {output_path}")


if __name__ == "__main__":
    main()
