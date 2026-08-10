#!/usr/bin/env bash
# One-time environment setup: creates a venv and installs deps + the
# Playwright-managed Chromium build used for AAD login automation.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

python3 -m venv "$SCRIPT_DIR/.venv"
"$SCRIPT_DIR/.venv/bin/pip" install --upgrade pip
"$SCRIPT_DIR/.venv/bin/pip" install -r "$SCRIPT_DIR/requirements.txt"
"$SCRIPT_DIR/.venv/bin/playwright" install chromium

echo
echo "Setup complete. Run: ./rdp-connect.sh <host>"
