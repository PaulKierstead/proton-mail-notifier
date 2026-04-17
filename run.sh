#!/bin/bash
# Wrapper invoked by launchd. Keeps secrets out of the plist and activates the venv.
set -euo pipefail

APP_DIR="$HOME/opt/proton-watcher"
CONF_DIR="$HOME/.config/proton-watcher"

# shellcheck disable=SC1091
[[ -f "$CONF_DIR/env" ]] && source "$CONF_DIR/env"

# Activate venv if present; otherwise fall back to system python.
if [[ -f "$APP_DIR/.venv/bin/activate" ]]; then
    # shellcheck disable=SC1091
    source "$APP_DIR/.venv/bin/activate"
fi

exec python "$APP_DIR/watcher.py" --config "$CONF_DIR/config.yaml"
