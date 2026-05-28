#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="/opt/flowlogin"
SERVICE_USER="${SUDO_USER:-$(whoami)}"

if [[ $EUID -ne 0 ]]; then
    echo "[-] Run with sudo: sudo bash install.sh"
    exit 1
fi

echo "[*] Installing to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
cp -r . "$INSTALL_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

echo "[*] Installing uv for $SERVICE_USER..."
sudo -u "$SERVICE_USER" bash -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
UV_BIN="/home/$SERVICE_USER/.local/bin/uv"

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
