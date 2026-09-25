"""CPU (py-spy) and I/O (strace) profiling for the TUI.

Ported from ``profiling/run_pyspy.sh`` and ``profiling/run_strace.sh``.
"""

from __future__ import annotations

import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from .common import PROJECT_DIR

OUTPUT_DIR = PROJECT_DIR / "profiling" / "output"
APP_PATH = PROJECT_DIR / "signal_tui.py"


def _python_bin() -> str:
    venv_python = PROJECT_DIR / ".venv" / "bin" / "python"
    return str(venv_python) if venv_python.is_file() else sys.executable


def _pyspy_bin() -> str | None:
    venv_pyspy = PROJECT_DIR / ".venv" / "bin" / "py-spy"
    if venv_pyspy.is_file():
        return str(venv_pyspy)
    return shutil.which("py-spy")


def run_pyspy(duration: int) -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    flamegraph = OUTPUT_DIR / "flamegraph.svg"

    pyspy_bin = _pyspy_bin()
    if not pyspy_bin:
        print("❌ py-spy is not installed. Install it with:")
        print("   pip install -r profiling/requirements.txt")
        return 1
    print(f"   Using py-spy: {pyspy_bin}")

    if not APP_PATH.is_file():
        print(f"❌ App not found: {APP_PATH}")
        return 1

    print("🚀 Starting Signal TUI for py-spy sampling...")
    print(f"   Duration: {duration}s")
    print(f"   Output:   {flamegraph}")
    print()
    print("   ⚠️  Use the app normally during sampling (send/receive messages,")
    print("       switch contacts, open chats, etc.)")
    print()

    proc = subprocess.Popen([_python_bin(), str(APP_PATH)])
    time.sleep(3)
    if proc.poll() is not None:
        print("❌ App failed to start. Check for errors.")
        return 1

    print(f"   App started with PID: {proc.pid}")
    print(f"   Sampling for {duration}s...")
    print()

    result = subprocess.run(
        [
            pyspy_bin,
            "record",
            "--pid",
            str(proc.pid),
            "--duration",
            str(duration),
            "--output",
            str(flamegraph),
        ],
        check=False,
    )
    if result.returncode != 0:
        print(
            "❌ py-spy failed. Make sure you have permission to attach to the process."
        )
        print("   Try running with sudo: sudo py-spy record ...")
        proc.terminate()
        return 1

    try:
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=15)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass

    print()
    print(f"✅ Flamegraph saved to: {flamegraph}")
    print()
    print("📊 To view the flamegraph (open in browser):")
    print(f"   xdg-open {flamegraph}")
    return 0


def run_strace(duration: int) -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    strace_log = OUTPUT_DIR / "strace.log"
    strace_summary = OUTPUT_DIR / "strace_summary.txt"

    if not shutil.which("strace"):
        print("❌ strace is not installed. Install it with:")
        print("   sudo apt install strace")
        return 1
    if not shutil.which("timeout"):
        print("❌ 'timeout' (coreutils) is not installed.")
        return 1

    if not APP_PATH.is_file():
        print(f"❌ App not found: {APP_PATH}")
        return 1

    print("🚀 Starting Signal TUI under strace...")
    print(f"   Duration: {duration}s")
    print(f"   Output:   {strace_log}")
    print()
    print("   ⚠️  Use the app normally during tracing (send/receive messages,")
    print("       switch contacts, open chats, etc.)")
    print()

    # `timeout -s INT` on the *app* (not on strace) so SIGINT reaches Python
    # directly, letting Textual exit cleanly and restore the terminal.
    app_proc = subprocess.Popen(
        ["timeout", "-s", "INT", str(duration), _python_bin(), str(APP_PATH)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    strace_proc = subprocess.Popen(
        [
            "strace",
            "-f",
            "-c",
            "-T",
            "-s",
            "256",
            "-e",
            "trace=file,read,write,network",
            "-o",
            str(strace_log),
            "-p",
            str(app_proc.pid),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    app_proc.wait()
    time.sleep(1)
    strace_proc.terminate()
    try:
        strace_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        strace_proc.kill()

    print()
    print("✅ Trace complete. Analyzing...")

    log_text = (
        strace_log.read_text(encoding="utf-8", errors="replace")
        if strace_log.exists()
        else ""
    )

    lines = [
        "=============================================",
        "  SIGNAL TUI CLIENT — STRACE SUMMARY",
        "=============================================",
        "",
        "--- TOP 20 FILES OPENED ---",
    ]
    files = re.findall(r'openat\([^,]+,\s*"([^"]+)"', log_text)
    counts: dict[str, int] = {}
    for f in files:
        counts[f] = counts.get(f, 0) + 1
    for name, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:20]:
        lines.append(f"{count:>7} {name}")

    lines += ["", "--- CACHE FILE ACCESS COUNT ---"]
    cache_file = (
        Path.home() / ".local" / "share" / "signal-tui-client" / "messages.json"
    )
    if cache_file.is_file():
        open_count = log_text.count("messages.json")
        lines.append(f"  messages.json opened: {open_count} times")
    else:
        lines.append(f"  Cache file not found at: {cache_file}")

    lines += ["", "--- SYSCALL SUMMARY (from strace -c) ---"]
    m = re.search(r"^% time.*", log_text, re.MULTILINE)
    if m:
        block = log_text[m.start() :].splitlines()[:31]
        lines.extend(block)

    strace_summary.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print()
    print(f"✅ Summary saved to: {strace_summary}")
    print()
    print("📄 To view the summary:")
    print(f"   cat {strace_summary}")
    print()
    print("📄 To view the full trace:")
    print(f"   less {strace_log}")
    return 0
