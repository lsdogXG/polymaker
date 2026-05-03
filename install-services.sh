#!/bin/bash
#
# Polymaker Service Installation Script
# Run with: sudo bash install-services.sh
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="/var/log/polymaker"

echo "=========================================="
echo "  Polymaker Service Installer"
echo "=========================================="

# Create log directory
echo "[1/5] Creating log directory..."
mkdir -p "$LOG_DIR"
chmod 755 "$LOG_DIR"

# Stop existing processes
echo "[2/5] Stopping existing processes..."
pkill -9 -f "python.*app.main" 2>/dev/null || true
pkill -9 -f "serve_frontend.py" 2>/dev/null || true
sleep 2

# Copy service files
echo "[3/5] Installing systemd service files..."
cp "$SCRIPT_DIR/polymaker.service" /etc/systemd/system/
cp "$SCRIPT_DIR/polymaker-frontend.service" /etc/systemd/system/
chmod 644 /etc/systemd/system/polymaker.service
chmod 644 /etc/systemd/system/polymaker-frontend.service

# Reload systemd
echo "[4/5] Reloading systemd daemon..."
systemctl daemon-reload

# Enable services
echo "[5/5] Enabling services..."
systemctl enable polymaker.service
systemctl enable polymaker-frontend.service

echo ""
echo "=========================================="
echo "  Installation Complete!"
echo "=========================================="
echo ""
echo "Commands:"
echo "  Start:   systemctl start polymaker polymaker-frontend"
echo "  Stop:    systemctl stop polymaker polymaker-frontend"
echo "  Status:  systemctl status polymaker polymaker-frontend"
echo "  Logs:    journalctl -u polymaker -f"
echo "           tail -f /var/log/polymaker/polymaker.log"
echo ""
echo "To start now:"
echo "  systemctl start polymaker polymaker-frontend"
echo ""
