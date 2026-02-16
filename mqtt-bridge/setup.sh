#!/bin/bash
# Meshtastic MQTT Bridge Setup Script
# Installs Mosquitto, configures bridge to mqtt.meshtastic.org, and sets up systemd service

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/config.json"
MOSQUITTO_CONF="$SCRIPT_DIR/mosquitto.conf"

echo "================================"
echo "Meshtastic MQTT Bridge Setup"
echo "================================"
echo ""

# Check if running as root
if [ "$EUID" -ne 0 ]; then 
    echo "This script must be run as root (use sudo)"
    exit 1
fi

# Detect OS
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS=$ID
else
    echo "Cannot detect OS. This script supports Ubuntu/Debian."
    exit 1
fi

echo "Detected OS: $OS"
echo ""

# Install Mosquitto
echo "Installing Mosquitto MQTT broker..."
if [ "$OS" = "ubuntu" ] || [ "$OS" = "debian" ]; then
    apt-get update
    apt-get install -y mosquitto mosquitto-clients
else
    echo "Unsupported OS. Please install mosquitto manually."
    exit 1
fi

# Stop mosquitto service
systemctl stop mosquitto || true

# Read configuration
if [ ! -f "$CONFIG_FILE" ]; then
    echo "Error: config.json not found!"
    exit 1
fi

USERNAME=$(grep -o '"username": *"[^"]*"' "$CONFIG_FILE" | cut -d'"' -f4)
PASSWORD=$(grep -o '"password": *"[^"]*"' "$CONFIG_FILE" | cut -d'"' -f4)

echo ""
echo "Configuration:"
echo "  Username: $USERNAME"
echo "  Password: ********"
echo ""

# Create password file
echo "Creating Mosquitto password file..."
mosquitto_passwd -c -b /etc/mosquitto/passwd "$USERNAME" "$PASSWORD"

# Generate mosquitto.conf with topics from config.json
echo "Generating Mosquitto configuration..."
python3 << 'PYEOF'
import json

with open("$CONFIG_FILE", 'r') as f:
    config = json.load(f)

topics = config.get('bridge', {}).get('topics', ['msh/2/e/#'])

conf = """# Mosquitto MQTT Broker Configuration for Meshtastic Bridge
# This broker bridges to the public mqtt.meshtastic.org server

# Listener configuration
listener 1883
protocol mqtt

# Authentication
allow_anonymous false
password_file /etc/mosquitto/passwd

# Bridge to public Meshtastic MQTT server
connection meshtastic-bridge
address mqtt.meshtastic.org:1883
"""

for topic in topics:
    conf += f"topic {topic} both 0\n"

conf += """cleansession false
try_private false
notifications false
bridge_attempt_unsubscribe false
"""

with open("/etc/mosquitto/conf.d/meshtastic-bridge.conf", 'w') as f:
    f.write(conf)

print(f"Configured {len(topics)} topic(s):")
for topic in topics:
    print(f"  - {topic}")
PYEOF

# Create log directory
mkdir -p /var/log/mosquitto
chown mosquitto:mosquitto /var/log/mosquitto

# Enable and start service
echo "Enabling and starting Mosquitto service..."
systemctl enable mosquitto
systemctl restart mosquitto

# Wait for service to start
sleep 2

# Check status
if systemctl is-active --quiet mosquitto; then
    echo ""
    echo "✓ Mosquitto MQTT bridge is running!"
    echo ""
    echo "Connection details for your Meshtastic devices:"
    echo "  MQTT Server: localhost (or this machine's IP)"
    echo "  Port: 1883"
    echo "  Username: $USERNAME"
    echo "  Password: $PASSWORD"
    echo "  Encryption: Disabled (local network)"
    echo ""
    echo "The bridge is now syncing with mqtt.meshtastic.org"
    echo "Configure your Meshtastic devices to use this broker."
else
    echo ""
    echo "✗ Failed to start Mosquitto service"
    echo "Check logs: journalctl -u mosquitto -n 50"
    exit 1
fi

echo ""
echo "Setup complete!"
