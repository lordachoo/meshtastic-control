#!/bin/bash
# Update MQTT Bridge Configuration
# Updates username/password and restarts the service

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/config.json"

# Check if running as root
if [ "$EUID" -ne 0 ]; then 
    echo "This script must be run as root (use sudo)"
    exit 1
fi

# Check if mosquitto is installed
if ! command -v mosquitto_passwd &> /dev/null; then
    echo "Mosquitto is not installed. Run setup.sh first."
    exit 1
fi

echo "================================"
echo "Update MQTT Bridge Credentials"
echo "================================"
echo ""

# Read current config
if [ ! -f "$CONFIG_FILE" ]; then
    echo "Error: config.json not found!"
    exit 1
fi

CURRENT_USER=$(grep -o '"username": *"[^"]*"' "$CONFIG_FILE" | cut -d'"' -f4)
echo "Current username: $CURRENT_USER"
echo ""

# Prompt for new credentials
read -p "Enter new username (or press Enter to keep '$CURRENT_USER'): " NEW_USER
if [ -z "$NEW_USER" ]; then
    NEW_USER="$CURRENT_USER"
fi

read -sp "Enter new password: " NEW_PASS
echo ""

if [ -z "$NEW_PASS" ]; then
    echo "Error: Password cannot be empty"
    exit 1
fi

# Update config.json
echo "Updating config.json..."
sed -i "s/\"username\": \"[^\"]*\"/\"username\": \"$NEW_USER\"/" "$CONFIG_FILE"
sed -i "s/\"password\": \"[^\"]*\"/\"password\": \"$NEW_PASS\"/" "$CONFIG_FILE"

# Update mosquitto password file
echo "Updating Mosquitto password file..."
mosquitto_passwd -c -b /etc/mosquitto/passwd "$NEW_USER" "$NEW_PASS"

# Restart service
echo "Restarting Mosquitto service..."
systemctl restart mosquitto

sleep 2

if systemctl is-active --quiet mosquitto; then
    echo ""
    echo "✓ Configuration updated successfully!"
    echo ""
    echo "New connection details:"
    echo "  Username: $NEW_USER"
    echo "  Password: ********"
    echo ""
    echo "Update your Meshtastic devices with the new credentials."
else
    echo ""
    echo "✗ Failed to restart Mosquitto service"
    echo "Check logs: journalctl -u mosquitto -n 50"
    exit 1
fi
