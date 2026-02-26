#!/usr/bin/env bash
# gdbAssist.sh - Wrapper script for gdbAssist.py
#
# Usage:
#   ./gdbAssist.sh --type TYPE_NAME --gdb GDB_OUTPUT_FILE
#                  [--build-dir BUILD_DIR] [--bits 32|64]
#                  [--src-dirs DIR1,DIR2,...] [--output OUTPUT_FILE]
#                  [--var-name VAR_NAME]
#
# Cross-platform: works on Linux and Windows (Git Bash / WSL).

set -euo pipefail

# ---------------------------------------------------------------------------
# Locate Python 3
# ---------------------------------------------------------------------------
find_python() {
    local candidates=("python3" "python" "python3.exe" "python.exe")
    for cmd in "${candidates[@]}"; do
        if command -v "$cmd" &>/dev/null; then
            local ver
            ver=$("$cmd" -c "import sys; print(sys.version_info.major)" 2>/dev/null || echo "0")
            if [ "$ver" = "3" ]; then
                echo "$cmd"
                return 0
            fi
        fi
    done
    echo ""
}

PYTHON=$(find_python)

if [ -z "$PYTHON" ]; then
    echo "[error] Python 3 is required but not found." >&2
    echo "  Install Python 3 from https://www.python.org/downloads/" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Locate gdbAssist.py (same directory as this script)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GDBASST_PY="$SCRIPT_DIR/gdbAssist.py"

if [ ! -f "$GDBASST_PY" ]; then
    echo "[error] gdbAssist.py not found at: $GDBASST_PY" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Pass all arguments to Python script
# ---------------------------------------------------------------------------
exec "$PYTHON" "$GDBASST_PY" "$@"
