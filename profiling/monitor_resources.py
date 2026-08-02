#!/usr/bin/env python3
"""
Resource Monitoring for Signal TUI Client using psutil.

This script launches signal_tui.py as a subprocess and samples CPU, RAM,
and I/O usage at regular intervals. It also monitors the signal-cli daemon
process if it's running.

Output is written to a CSV file for easy analysis and plotting.

Usage:
    python profiling/monitor_resources.py [--duration SECONDS] [--interval SECONDS]
                                          [--output PATH]

Options:
    --duration SECONDS    How long to monitor (default: 120)
    --interval SECONDS    Sampling interval (default: 2)
    --output PATH         Output CSV path (default: profiling/output/resources.csv)

Examples:
    # Monitor for 2 minutes, sampling every 2 seconds
    python profiling/monitor_resources.py --duration 120

    # Monitor for 5 minutes, sampling every 1 second
    python profiling/monitor_resources.py --duration 300 --interval 1
"""

import argparse
import csv
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


try:
    import psutil
except ImportError:
    print("❌ psutil is not installed. Install it with:")
    print("   pip install -r profiling/requirements.txt")
    sys.exit(1)

# Project root (parent of profiling/)
PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_OUTPUT_DIR = Path(__file__).parent / "output"


def parse_args():
    parser = argparse.ArgumentParser(description="Resource monitoring for Signal TUI Client")
    parser.add_argument(
        "--duration",
        type=int,
        default=120,
        help="How long to monitor in seconds (default: 120)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Sampling interval in seconds (default: 2)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=str(DEFAULT_OUTPUT_DIR / "resources.csv"),
        help="Output CSV path",
    )
    return parser.parse_args()


def find_signal_cli_processes():
    """Find signal-cli daemon processes.

    The daemon is a Java process (``java``) whose command line contains
    ``signal-cli``.  We match on the command line rather than the process
    name, because the JVM process is named ``java``.
    """
    cli_procs = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmdline = proc.info.get("cmdline") or []
            name = proc.info.get("name") or ""
            # Match either a process literally named signal-cli, or any
            # process (e.g. java) whose command line references signal-cli.
            if "signal-cli" in name or any("signal-cli" in c for c in cmdline):
                cli_procs.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return cli_procs



def get_process_info(proc):
    """Get CPU%, RSS, VMS, and I/O info for a process."""
    try:
        cpu_percent = proc.cpu_percent(interval=None)
        mem = proc.memory_info()
        io = proc.io_counters() if hasattr(proc, "io_counters") else None
        return {
            "cpu": cpu_percent,
            "rss_mb": mem.rss / 1024 / 1024,
            "vms_mb": mem.vms / 1024 / 1024,
            "read_bytes": io.read_bytes if io else 0,
            "write_bytes": io.write_bytes if io else 0,
            "num_threads": proc.num_threads(),
            "num_fds": proc.num_fds() if hasattr(proc, "num_fds") else 0,
        }
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def main():
    args = parse_args()

    # Ensure output directory exists
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Path to the main app
    app_path = PROJECT_ROOT / "signal_tui.py"
    if not app_path.exists():
        print(f"❌ App not found: {app_path}")
        sys.exit(1)

    print(f"🚀 Starting Signal TUI with resource monitoring...")
    print(f"   Duration:  {args.duration}s")
    print(f"   Interval:  {args.interval}s")
    print(f"   Output:    {output_path}")
    print()
    print("   ⚠️  Use the app normally during monitoring (send/receive messages,")
    print("       switch contacts, open chats, etc.)")
    print()

    # Use the .venv Python if available
    venv_python = PROJECT_ROOT / ".venv" / "bin" / "python"
    python_bin = venv_python if venv_python.exists() else Path(sys.executable)

    # Launch the app
    proc = subprocess.Popen(
        [str(python_bin), str(app_path)],
        cwd=str(PROJECT_ROOT),
    )


    # Wrap in psutil
    app_psutil = psutil.Process(proc.pid)

    # Priming call: psutil.cpu_percent() returns 0.0 on the first call.
    # Calling it once before the loop ensures subsequent samples are real.
    app_psutil.cpu_percent(interval=None)

    # Track previous I/O counters for delta calculation
    prev_io = {"read": 0, "write": 0}
    prev_cli_io = {"read": 0, "write": 0}


    # Open CSV file
    with open(output_path, "w", newline="") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow([
            "timestamp",
            "elapsed_s",
            "app_cpu_pct",
            "app_rss_mb",
            "app_vms_mb",
            "app_read_mb_s",
            "app_write_mb_s",
            "app_threads",
            "app_fds",
            "cli_cpu_pct",
            "cli_rss_mb",
            "cli_read_mb_s",
            "cli_write_mb_s",
            "cli_threads",
            "cli_fds",
        ])

        start_time = time.time()
        last_sample = start_time

        try:
            while time.time() - start_time < args.duration:
                # Check if app is still running
                if proc.poll() is not None:
                    print(f"⚠️  App exited early with code {proc.returncode}")
                    break

                now = time.time()
                elapsed = now - start_time

                # Get app process info
                app_info = get_process_info(app_psutil)

                # Get signal-cli daemon info
                cli_procs = find_signal_cli_processes()
                cli_info = None
                if cli_procs:
                    cli_info = get_process_info(cli_procs[0])

                # Calculate I/O rates (MB/s since last sample)
                dt = now - last_sample
                if app_info:
                    read_rate = (app_info["read_bytes"] - prev_io["read"]) / dt / 1024 / 1024
                    write_rate = (app_info["write_bytes"] - prev_io["write"]) / dt / 1024 / 1024
                    prev_io["read"] = app_info["read_bytes"]
                    prev_io["write"] = app_info["write_bytes"]
                else:
                    read_rate = write_rate = 0

                cli_read_rate = cli_write_rate = 0
                if cli_info:
                    cli_read_rate = (cli_info["read_bytes"] - prev_cli_io["read"]) / dt / 1024 / 1024
                    cli_write_rate = (cli_info["write_bytes"] - prev_cli_io["write"]) / dt / 1024 / 1024
                    prev_cli_io["read"] = cli_info["read_bytes"]
                    prev_cli_io["write"] = cli_info["write_bytes"]

                # Write row
                row = [
                    f"{now:.1f}",
                    f"{elapsed:.1f}",
                    f"{app_info['cpu']:.1f}" if app_info else "N/A",
                    f"{app_info['rss_mb']:.1f}" if app_info else "N/A",
                    f"{app_info['vms_mb']:.1f}" if app_info else "N/A",
                    f"{read_rate:.3f}",
                    f"{write_rate:.3f}",
                    f"{app_info['num_threads']}" if app_info else "N/A",
                    f"{app_info['num_fds']}" if app_info else "N/A",
                    f"{cli_info['cpu']:.1f}" if cli_info else "N/A",
                    f"{cli_info['rss_mb']:.1f}" if cli_info else "N/A",
                    f"{cli_read_rate:.3f}",
                    f"{cli_write_rate:.3f}",
                    f"{cli_info['num_threads']}" if cli_info else "N/A",
                    f"{cli_info['num_fds']}" if cli_info else "N/A",
                ]
                writer.writerow(row)
                csvfile.flush()

                # Print a progress line every ~10 seconds
                if int(elapsed) % 10 == 0 and int(elapsed) != int(last_sample):
                    status = f"  [{elapsed:5.0f}s] CPU: {row[2]:>5}%  RSS: {row[3]:>7} MB"
                    if cli_info:
                        status += f"  CLI CPU: {row[9]:>5}%  CLI RSS: {row[10]:>7} MB"
                    print(status)

                last_sample = now
                time.sleep(args.interval)

        except KeyboardInterrupt:
            print("\n⏹️  Interrupted by user. Saving data...")
        finally:
            # Send SIGINT (Ctrl+C) so Textual can exit cleanly and restore
            # the terminal. SIGTERM would leave the terminal broken.
            try:
                proc.send_signal(signal.SIGINT)
                proc.wait(timeout=10)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


    print(f"\n✅ Resource data saved to: {output_path}")
    print()
    print("📊 To analyze the data:")
    print(f"   python profiling/analyze_resources.py --input {output_path}")
    print()
    print("   Or plot with gnuplot:")
    print(f"   gnuplot -e \"set datafile separator ','; plot '{output_path}' using 2:3 with lines\"")


if __name__ == "__main__":
    main()
