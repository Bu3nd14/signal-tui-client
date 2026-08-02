#!/usr/bin/env bash
# =============================================================================
# Flamegraph Profiling for Signal TUI Client using py-spy.
#
# py-spy is a sampling profiler that works on a running process without
# modifying the code. It generates a flamegraph SVG showing where CPU
# time is spent.
#
# Usage:
#   ./profiling/run_pyspy.sh [DURATION_SECONDS]
#
# Arguments:
#   DURATION_SECONDS   How long to sample (default: 120)
#
# Output:
#   profiling/output/flamegraph.svg  — Interactive flamegraph
#
# Requirements:
#   py-spy must be installed: pip install py-spy

# =============================================================================

set -euo pipefail

# Configuration
DURATION="${1:-120}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
OUTPUT_DIR="$SCRIPT_DIR/output"
APP_PATH="$PROJECT_ROOT/signal_tui.py"
FLAMEGRAPH="$OUTPUT_DIR/flamegraph.svg"


# Create output directory
mkdir -p "$OUTPUT_DIR"

# Check if py-spy is installed (prefer .venv, fallback to PATH)
PYSPY_BIN="$PROJECT_ROOT/.venv/bin/py-spy"
if [ ! -x "$PYSPY_BIN" ]; then
    PYSPY_BIN="$(command -v py-spy || true)"
fi
if [ -z "$PYSPY_BIN" ]; then
    echo "❌ py-spy is not installed. Install it with:"
    echo "   pip install -r profiling/requirements.txt"
    exit 1
fi
echo "   Using py-spy: $PYSPY_BIN"


# Check if the app exists
if [ ! -f "$APP_PATH" ]; then
    echo "❌ App not found: $APP_PATH"
    exit 1
fi

echo "🚀 Starting Signal TUI for py-spy sampling..."
echo "   Duration: ${DURATION}s"
echo "   Output:   $FLAMEGRAPH"
echo ""
echo "   ⚠️  Use the app normally during sampling (send/receive messages,"
echo "       switch contacts, open chats, etc.)"
echo ""

# Use the .venv Python if available
if [ -x "$PROJECT_ROOT/.venv/bin/python" ]; then
    PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python"
else
    PYTHON_BIN="python3"
fi

# Start the app in the background
"$PYTHON_BIN" "$APP_PATH" &
APP_PID=$!


# Wait a moment for the app to start
sleep 3

# Check if the app is still running
if ! kill -0 "$APP_PID" 2>/dev/null; then
    echo "❌ App failed to start. Check for errors."
    exit 1
fi

echo "   App started with PID: $APP_PID"
echo "   Sampling for ${DURATION}s..."
echo ""

# Record the flamegraph
"$PYSPY_BIN" record --pid "$APP_PID" --duration "$DURATION" --output "$FLAMEGRAPH" || {
    echo "❌ py-spy failed. Make sure you have permission to attach to the process."
    echo "   Try running with sudo: sudo py-spy record ..."
    kill "$APP_PID" 2>/dev/null || true
    exit 1
}

# Stop the app with SIGINT (Ctrl+C) so Textual can exit cleanly
# and restore the terminal. SIGTERM would leave the terminal broken.
kill -INT "$APP_PID" 2>/dev/null || true
wait "$APP_PID" 2>/dev/null || true



echo ""
echo "✅ Flamegraph saved to: $FLAMEGRAPH"
echo ""
echo "📊 To view the flamegraph (open in browser):"
echo "   xdg-open $FLAMEGRAPH"

