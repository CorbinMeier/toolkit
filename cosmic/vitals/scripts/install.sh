#!/usr/bin/env bash
# Builds the release binary and installs it to the user-local bin dir. Never touches system paths.
# With --service it also installs and enables the vitals-web.service user unit (the web dashboard, spec §14).
set -euo pipefail
cd "$(dirname "$0")/.."
service=0
for arg in "$@"; do
    case "$arg" in
        --service) service=1 ;;
        *) echo "usage: $0 [--service]" >&2; exit 2 ;;
    esac
done
cargo build --release
dest="${HOME}/.local/bin"
mkdir -p "$dest"
install -m 0755 target/release/vitals "$dest/vitals"
echo "installed $dest/vitals"
if [ "$service" -eq 1 ]; then
    unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    mkdir -p "$unit_dir"
    cat > "$unit_dir/vitals-web.service" <<'UNIT'
[Unit]
Description=vitals web dashboard
After=network-online.target

[Service]
ExecStart=%h/.local/bin/vitals --serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
UNIT
    systemctl --user daemon-reload
    systemctl --user enable --now vitals-web.service
    echo "installed and enabled $unit_dir/vitals-web.service"
fi
