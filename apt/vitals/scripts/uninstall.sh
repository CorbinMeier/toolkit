#!/usr/bin/env bash
# Removes the user-local binary and the vitals-web.service user unit if there is one.
# The token index in ~/.cache/vitals is left alone.
set -euo pipefail
unit="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/vitals-web.service"
if [ -f "$unit" ]; then
    systemctl --user disable --now vitals-web.service || true
    rm -f "$unit"
    systemctl --user daemon-reload || true
    echo "removed $unit"
fi
rm -f "${HOME}/.local/bin/vitals"
echo "removed ${HOME}/.local/bin/vitals"
