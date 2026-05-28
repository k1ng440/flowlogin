#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="/opt/flowlogin"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_USER="${SUDO_USER:-$(whoami)}"

if [[ $EUID -ne 0 ]]; then
    echo "[-] Run with sudo: sudo bash install.sh"
    exit 1
fi

echo "[*] Installing to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
if [[ "$SCRIPT_DIR" != "$INSTALL_DIR" ]]; then
    cp -r "$SCRIPT_DIR/." "$INSTALL_DIR/"
fi
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

echo "[*] Installing uv for $SERVICE_USER..."
sudo -u "$SERVICE_USER" bash -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'

# Resolve the actual uv path after install — login shell sources ~/.profile so PATH is correct
UV_BIN="$(sudo -u "$SERVICE_USER" bash -lc 'which uv 2>/dev/null')" || true
if [[ -z "$UV_BIN" ]]; then
    # Fallback: check common install locations
    for candidate in \
        "/home/$SERVICE_USER/.local/bin/uv" \
        "/home/$SERVICE_USER/.cargo/bin/uv" \
        "/usr/local/bin/uv"; do
        if [[ -x "$candidate" ]]; then
            UV_BIN="$candidate"
            break
        fi
    done
fi
if [[ -z "$UV_BIN" ]]; then
    echo "[-] Could not locate uv binary. Install manually and re-run."
    exit 1
fi
echo "[*] Found uv at $UV_BIN"

echo "[*] Installing Python deps..."
sudo -u "$SERVICE_USER" "$UV_BIN" pip install --python 3.11 playwright playwright-stealth httpx 2>/dev/null || true

echo "[*] Writing systemd units..."
cat > /etc/systemd/system/flowlogin.service <<EOF
[Unit]
Description=Flow2API session token updater

[Service]
Type=oneshot
User=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$UV_BIN run --script $INSTALL_DIR/login.py
StandardOutput=journal
StandardError=journal
EOF

cat > /etc/systemd/system/flowlogin.timer <<EOF
[Unit]
Description=Flow2API token refresh every 55 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=55min

[Install]
WantedBy=timers.target
EOF

echo "[*] Enabling timer..."
systemctl daemon-reload
systemctl enable --now flowlogin.timer

echo ""
echo "[+] Done."
echo "    Logs:   journalctl -u flowlogin.service -f"
echo "    Status: systemctl list-timers flowlogin.timer"
echo "    Test:   systemctl start flowlogin.service"
