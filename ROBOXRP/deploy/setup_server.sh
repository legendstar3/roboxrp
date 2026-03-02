#!/bin/bash
# ============================================================
# ROBOXRP Bot - Oracle Cloud Server Setup
# Run this ONCE on a fresh Ubuntu 22.04 VM
# Usage: bash setup_server.sh
# ============================================================
set -e

echo "=========================================="
echo " ROBOXRP Bot - Server Setup"
echo "=========================================="

# 1. System updates
echo "[1/6] Updating system..."
sudo apt update && sudo apt upgrade -y

# 2. Install Python 3.11+ and pip
echo "[2/6] Installing Python..."
sudo apt install -y python3 python3-pip python3-venv git

# 3. Create bot user (non-root for security)
echo "[3/6] Creating bot user..."
if ! id "botuser" &>/dev/null; then
    sudo useradd -m -s /bin/bash botuser
    echo "  Created user 'botuser'"
else
    echo "  User 'botuser' already exists"
fi

# 4. Create bot directory
echo "[4/6] Setting up bot directory..."
sudo mkdir -p /opt/roboxrp
sudo chown botuser:botuser /opt/roboxrp

# 5. Copy bot files (run this after scp)
echo "[5/6] Setting up Python environment..."
sudo -u botuser bash -c '
    cd /opt/roboxrp
    python3 -m venv venv
    source venv/bin/activate
    pip install --upgrade pip
    pip install -r requirements.txt
'

# 6. Create systemd service for auto-start + restart on crash
echo "[6/6] Creating systemd service..."
sudo tee /etc/systemd/system/roboxrp.service > /dev/null << 'EOF'
[Unit]
Description=ROBOXRP Pro Trading Bot v13.2
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=botuser
Group=botuser
WorkingDirectory=/opt/roboxrp/TradingBuddy/TradingBuddy
ExecStart=/opt/roboxrp/venv/bin/python unified_trading_system_WORKING.py
Restart=always
RestartSec=30
StartLimitIntervalSec=300
StartLimitBurst=5

# Environment
EnvironmentFile=/opt/roboxrp/.env

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=roboxrp

# Security hardening
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/opt/roboxrp

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable roboxrp.service

echo ""
echo "=========================================="
echo " SETUP COMPLETE!"
echo "=========================================="
echo ""
echo " Next steps:"
echo "   1. Copy bot files:  scp -r ... ubuntu@<IP>:/tmp/roboxrp/"
echo "   2. Move to /opt:    sudo cp -r /tmp/roboxrp/* /opt/roboxrp/"
echo "   3. Fix ownership:   sudo chown -R botuser:botuser /opt/roboxrp/"
echo "   4. Start bot:       sudo systemctl start roboxrp"
echo "   5. View logs:       sudo journalctl -u roboxrp -f"
echo "   6. Check status:    sudo systemctl status roboxrp"
echo ""
echo " The bot will auto-start on reboot and restart on crash."
echo "=========================================="
