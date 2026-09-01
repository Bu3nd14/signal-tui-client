#!/usr/bin/env python3
"""
Memory Profiling for Signal TUI Client using tracemalloc.

This script creates a wrapper that imports signal_tui.py and runs the app
in the SAME process with tracemalloc enabled. tracemalloc cannot profile
subprocesses from outside, so the app must run in-process.

The app is terminated with SIGINT (Ctrl+C) so that Textual can exit cleanly
and restore the terminal.

Usage:
    python profiling/profile_memory.py [--duration SECONDS] [--output PATH]

Options:
    --duration SECONDS   How long to profile (default: 120)
    --output PATH        Output report path (default: profiling/output/memory_report.txt)

Examples:
    # Profile for 2 minutes
    python profiling/profile_memory.py --duration 120

    # Profile for 5 minutes
    python profiling/profile_memory.py --duration 300
"""

import argparse
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Project root (parent of profiling/)
PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_OUTPUT_DIR = Path(__file__).parent / "output"


def parse_args():
    parser = argparse.ArgumentParser(description="Memory profiling for Signal TUI Client")
    parser.add_argument(
        "--duration",
        type=int,
        default=120,
        help="How long to profile in seconds (default: 120)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=str(DEFAULT_OUTPUT_DIR / "memory_report.txt"),
        help="Output report path",
    )
    return parser.parse_args()


def find_venv_python() -> Path:
    """Find the Python interpreter from the project's .venv."""
    venv_python = PROJECT_ROOT / ".venv" / "bin" / "python"
    if venv_python.exists():
        return venv_python
    # Fallback to the current interpreter
    return Path(sys.executable)


def create_wrapper_script(duration: int, output_path: Path) -> Path:
    """
    Create a temporary wrapper script that runs signal_tui.py in-process
    with tracemalloc enabled. This is necessary because tracemalloc cannot
    profile subprocesses from outside.
    """
    wrapper = tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, dir=str(PROJECT_ROOT)
    )
    wrapper.write(f'''#!/usr/bin/env python3
"""Temporary wrapper for memory profiling of Signal TUI Client."""

import os
import signal
import sys
import time
import tracemalloc
from pathlib import Path

# Add project root to path so imports work
PROJECT_ROOT = Path({str(PROJECT_ROOT)!r})
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(str(PROJECT_ROOT))

# Enable tracemalloc
tracemalloc.start(25)

# Import the app module (this runs the module-level code)
import signal_tui

# Acquire the lock (same as __main__ block in signal_tui.py)
if not signal_tui._acquire_lock():
    print("❌ Signal TUI is already running (lock file /tmp/signal-tui.lock).", file=sys.stderr)
    sys.exit(1)

# Create the app
app = signal_tui.SignalTUI()

def _handle_sigint(sig, frame):
    """Handle Ctrl+C: stop polling and exit cleanly."""
    app._polling_active = False
    app.exit()

signal.signal(signal.SIGINT, _handle_sigint)

# Take a baseline snapshot before the app runs
baseline_snapshot = tracemalloc.take_snapshot()

# Run the app
app.run()

# Release the lock
signal_tui._release_lock()

# Collect memory statistics
print("\\n📊 Collecting memory statistics...")
snapshot = tracemalloc.take_snapshot()
top_stats = snapshot.statistics("lineno")


output_path = Path({str(output_path)!r})
output_path.parent.mkdir(parents=True, exist_ok=True)

with open(output_path, "w") as f:
    f.write("=" * 80 + "\\n")
    f.write("  SIGNAL TUI CLIENT — MEMORY PROFILE REPORT\\n")
    f.write("=" * 80 + "\\n\\n")

    current, peak = tracemalloc.get_traced_memory()
    f.write(f"  Current memory: {{current / 1024 / 1024:.2f}} MB\\n")
    f.write(f"  Peak memory:    {{peak / 1024 / 1024:.2f}} MB\\n")
    f.write(f"  Duration:       {duration}s\\n")
    f.write("\\n")

    f.write("-" * 80 + "\\n")
    f.write("  TOP 50 MEMORY ALLOCATIONS (by line)\\n")
    f.write("-" * 80 + "\\n\\n")

    for i, stat in enumerate(top_stats[:50], 1):
        frame = stat.traceback[0]
        f.write(f"  #{{i}}: {{stat.size / 1024:.1f}} KiB — {{frame.filename}}:{{frame.lineno}}\\n")
        for line in stat.traceback[:3]:
            f.write(f"       {{line.filename}}:{{line.lineno}} — {{line}}...\\n")
        f.write("\\n")

    # Filter for project-specific files
    f.write("-" * 80 + "\\n")
    f.write("  PROJECT-SPECIFIC ALLOCATIONS (signal_tui, protocols, ui_components)\\n")
    f.write("-" * 80 + "\\n\\n")

    project_files = ("signal_tui.py", "protocols/", "ui_components.py", "emoji_picker.py")
    project_stats = [s for s in top_stats if any(pf in s.traceback[0].filename for pf in project_files)]

    if project_stats:
        for i, stat in enumerate(project_stats[:30], 1):
            frame = stat.traceback[0]
            f.write(f"  #{{i}}: {{stat.size / 1024:.1f}} KiB — {{frame.filename}}:{{frame.lineno}}\\n")
            for line in stat.traceback[:3]:
                f.write(f"       {{line.filename}}:{{line.lineno}} — {{line}}...\\n")
            f.write("\\n")
    else:
        f.write("  No project-specific allocations found.\\n")

    # Check for potential memory leaks by comparing the baseline snapshot
    # (taken before app.run()) with the final snapshot (taken after).
    f.write("-" * 80 + "\\n")
    f.write("  MEMORY LEAK DETECTION\\n")
    f.write("-" * 80 + "\\n\\n")

    baseline_stats = baseline_snapshot.statistics("lineno")
    size_baseline = sum(s.size for s in baseline_stats)
    size_final = sum(s.size for s in top_stats)
    growth = size_final - size_baseline

    f.write(f"  Baseline allocation size (before app.run): {{size_baseline / 1024 / 1024:.2f}} MB\\n")
    f.write(f"  Final allocation size (after app.run):      {{size_final / 1024 / 1024:.2f}} MB\\n")
    f.write(f"  Growth: {{growth / 1024 / 1024:.2f}} MB\\n\\n")

    if growth > 50 * 1024 * 1024:  # > 50 MB growth
        f.write("  ⚠️  Significant memory growth detected — possible leak!\\n")
    elif growth > 10 * 1024 * 1024:  # > 10 MB growth
        f.write("  ℹ️  Moderate memory growth — normal for a long-running app.\\n")
    else:
        f.write("  ✅ No significant memory growth detected.\\n")


    f.write("\\n" + "=" * 80 + "\\n")
    f.write("  END OF REPORT\\n")
    f.write("=" * 80 + "\\n")

print(f"✅ Memory report saved to: {{output_path}}")
''')
    wrapper.close()
    return Path(wrapper.name)


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

    # Use the .venv Python if available
    python_bin = find_venv_python()

    print(f"🚀 Starting Signal TUI under tracemalloc...")
    print(f"   Duration: {args.duration}s")
    print(f"   Python:   {python_bin}")
    print(f"   Output:   {output_path}")
    print()
    print("   ⚠️  Use the app normally during profiling (send/receive messages,")
    print("       switch contacts, open chats, etc.)")
    print()

    # Create the wrapper script
    wrapper_path = create_wrapper_script(args.duration, output_path)

    try:
        # Launch the wrapper (which runs the app in-process with tracemalloc)
        proc = subprocess.Popen(
            [str(python_bin), str(wrapper_path)],
            cwd=str(PROJECT_ROOT),
        )

        try:
            # Wait for the specified duration
            try:
                proc.wait(timeout=args.duration)
            except subprocess.TimeoutExpired:
                # Time's up — send SIGINT (Ctrl+C) so Textual can exit cleanly
                # and restore the terminal.
                print("\n⏹️  Time's up. Sending SIGINT to exit cleanly...")
                proc.send_signal(signal.SIGINT)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    # If SIGINT doesn't work, force kill
                    print("⚠️  App didn't exit cleanly. Forcing kill...")
                    proc.kill()
                    proc.wait()
        except KeyboardInterrupt:
            print("\n⏹️  Interrupted by user. Sending SIGINT...")
            try:
                proc.send_signal(signal.SIGINT)
                proc.wait(timeout=10)
            except Exception:
                try:
                    proc.kill()
                    proc.wait()
                except Exception:
                    pass
    finally:
        # Clean up the wrapper script
        try:
            wrapper_path.unlink()
        except Exception:
            pass

    # Check if the report was created
    if not output_path.exists():
        print(f"❌ Memory report not created. The app may have crashed.")
        print(f"   Check for errors above.")
        sys.exit(1)

    print(f"✅ Memory report saved to: {output_path}")
    print()
    print("📄 To view the report:")
    print(f"   cat {output_path}")


if __name__ == "__main__":
    main()
