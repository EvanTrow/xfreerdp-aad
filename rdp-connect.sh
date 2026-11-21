#!/usr/bin/env bash
# Connect to an Azure AD-secured RDP host, automating cert trust and AAD login.
#
# Usage: ./rdp-connect.sh <host> <1pass_item_uuid> [--desktop <index>] [extra xfreerdp args...]
#
# --desktop <index> moves the RDP window to that virtual desktop/workspace
# once it opens and switches to it (same effect as `wmctrl -s <index>`).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/.venv"

if [[ $# -lt 2 ]]; then
    echo "Usage: $0 <host> <1pass_item_uuid> [--desktop <index>] [extra xfreerdp args...]" >&2
    exit 1
fi

if [[ ! -x "$VENV/bin/python" ]]; then
    echo "Python venv not found at $VENV - run ./setup.sh first." >&2
    exit 1
fi

HOST="$1"
export RDP_AAD_OP_ITEM="$2"
shift 2

DESKTOP=""
PASSTHROUGH=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --desktop)
            if [[ $# -lt 2 ]]; then
                echo "--desktop requires a virtual desktop index" >&2
                exit 1
            fi
            DESKTOP="$2"
            shift 2
            ;;
        *)
            PASSTHROUGH+=("$1")
            shift
            ;;
    esac
done

# wmctrl matches windows by WM_CLASS, not by PID or title, so give this
# session's xfreerdp window a distinctive class to move precisely - not
# just whatever xfreerdp window happens to be around.
WM_CLASS="xfreerdp-aad-${HOST//[.:]/-}"
export RDP_AAD_WM_CLASS="$WM_CLASS"
[[ -n "$DESKTOP" ]] && export RDP_AAD_DESKTOP="$DESKTOP"

exec "$VENV/bin/python" "$SCRIPT_DIR/aad_login_bot.py" \
    /v:"$HOST" /sec:aad /cert:tofu /multimon /sound:sys:pulse /fonts /network:auto \
    /wm-class:"$WM_CLASS" "${PASSTHROUGH[@]}"
