#!/usr/bin/env bash
# =============================================================================
# I/O Profiling for Signal TUI Client using strace.
#
# Traces file I/O syscalls (open, read, write, close) of the app and
# produces a summary of how many times each file is accessed.
#
# Usage:
#   ./profiling/run_strace.sh [DURATION_SECONDS]
#
# Arguments:
#   DURATION_SECONDS   How long to trace (default: 120)
#
# Output:
#   profiling/output/strace.log          — Full syscall trace
#   profiling/output/strace_summary.txt  — Summary of syscalls per file
#
# Requirements:
#   strace must be installed: sudo apt install strace
# =============================================================================

set -euo pipefail

# Configuration
DURATION="${1:-120}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
OUTPUT_DIR="$SCRIPT_DIR/output"
APP_PATH="$PROJECT_ROOT/signal_tui.py"
STRACE_LOG="$OUTPUT_DIR/strace.log"
STRACE_SUMMARY="$OUTPUT_DIR/strace_summary.txt"

# Create output directory
mkdir -p "$OUTPUT_DIR"

# Check if strace is installed
if ! command -v strace &> /dev/null; then
    echo "❌ strace is not installed. Install it with:"
    echo "   sudo apt install strace"
    exit 1
fi

# Check if the app exists
if [ ! -f "$APP_PATH" ]; then
    echo "❌ App not found: $APP_PATH"
    exit 1
fi

echo "🚀 Starting Signal TUI under strace..."
echo "   Duration: ${DURATION}s"
echo "   Output:   $STRACE_LOG"
echo ""
echo "   ⚠️  Use the app normally during tracing (send/receive messages,"
echo "       switch contacts, open chats, etc.)"
echo ""

# Use the .venv Python if available
if [ -x "$PROJECT_ROOT/.venv/bin/python" ]; then
    PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python"
else
    PYTHON_BIN="python3"
fi

# Run the app under strace, tracing file I/O and network syscalls.
# -f: follow child processes (signal-cli daemon)
# -c: count syscalls and produce a summary at the end
# -e trace=file,read,write,network: trace file, read/write, and network syscalls
# -o: output to file
# -T: show time spent in each syscall
# -s 256: show up to 256 chars of string arguments (filenames)
#
# NOTE: We use `timeout -s INT` on the *app* (not on strace) so that SIGINT
# reaches the Python process directly, letting Textual exit cleanly and
# restore the terminal. strace itself is killed afterwards.
timeout -s INT "$DURATION" "$PYTHON_BIN" "$APP_PATH" 2>/dev/null &
APP_PID=$!

# Attach strace to the app process (and its children)
strace -f -c -T -s 256 \
    -e trace=file,read,write,network \
    -o "$STRACE_LOG" \
    -p "$APP_PID" 2>/dev/null &
STRACE_PID=$!

# Wait for the app to finish (timeout sends SIGINT after DURATION)
wait "$APP_PID" 2>/dev/null || true

# Give strace a moment to flush its output, then stop it
sleep 1
kill "$STRACE_PID" 2>/dev/null || true
wait "$STRACE_PID" 2>/dev/null || true


echo ""
echo "✅ Trace complete. Analyzing..."

# Generate a summary of file access counts
echo "=============================================" > "$STRACE_SUMMARY"
echo "  SIGNAL TUI CLIENT — STRACE SUMMARY" >> "$STRACE_SUMMARY"
echo "=============================================" >> "$STRACE_SUMMARY"
echo "" >> "$STRACE_SUMMARY"

# Count file opens by filename (openat has the filename as a string arg)
echo "--- TOP 20 FILES OPENED ---" >> "$STRACE_SUMMARY"
grep -oP 'openat\([^,]+,\s*"[^"]+"' "$STRACE_LOG" \
    | sed 's/.*"\([^"]*\)"/\1/' \
    | sort | uniq -c | sort -rn | head -20 >> "$STRACE_SUMMARY"

echo "" >> "$STRACE_SUMMARY"
echo "--- CACHE FILE ACCESS COUNT ---" >> "$STRACE_SUMMARY"
CACHE_FILE="$HOME/.local/share/signal-tui-client/messages.json"
if [ -f "$CACHE_FILE" ]; then
    OPEN_COUNT=$(grep -c "messages.json" "$STRACE_LOG" || echo 0)
    echo "  messages.json opened: $OPEN_COUNT times" >> "$STRACE_SUMMARY"
else
    echo "  Cache file not found at: $CACHE_FILE" >> "$STRACE_SUMMARY"
fi

echo "" >> "$STRACE_SUMMARY"
echo "--- SYSCALL SUMMARY (from strace -c) ---" >> "$STRACE_SUMMARY"
# Extract the summary section from strace output
grep -A 30 "^% time" "$STRACE_LOG" >> "$STRACE_SUMMARY" || true

echo ""
echo "✅ Summary saved to: $STRACE_SUMMARY"
echo ""
echo "📄 To view the summary:"
echo "   cat $STRACE_SUMMARY"
echo ""
echo "📄 To view the full trace:"
echo "   less $STRACE_LOG"
