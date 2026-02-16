#!/bin/bash
# Update MQTT Bridge Topic Subscriptions
# Allows you to customize which topics to subscribe to from mqtt.meshtastic.org

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/config.json"
MOSQUITTO_CONF="/etc/mosquitto/conf.d/meshtastic-bridge.conf"

# Check if running as root
if [ "$EUID" -ne 0 ]; then 
    echo "This script must be run as root (use sudo)"
    exit 1
fi

echo "================================"
echo "Update MQTT Bridge Topics"
echo "================================"
echo ""
echo "Current topics from config.json:"
echo ""

# Show current topics
grep -A 10 '"topics"' "$CONFIG_FILE" | grep '"msh' | sed 's/.*"\(msh[^"]*\)".*/  - \1/'

echo ""
echo "Common topic patterns:"
echo "  msh/US/#                    - All US region traffic (LOTS of data)"
echo "  msh/2/e/#                   - All encrypted messages (still a lot)"
echo "  msh/2/e/!YOUR_NODE_ID/#     - Only messages to/from your node"
echo "  msh/2/c/LongFast/#          - Specific channel name"
echo "  msh/2/json/#                - JSON formatted messages only"
echo ""
echo "You can use wildcards:"
echo "  #  - matches everything after"
echo "  +  - matches one level"
echo ""

read -p "Do you want to update topics? (y/n): " confirm
if [ "$confirm" != "y" ]; then
    echo "Cancelled."
    exit 0
fi

echo ""
echo "Enter topics one per line. Press Enter on empty line when done."
echo "Example: msh/2/e/!02ed4dfc/#"
echo ""

topics=()
while true; do
    read -p "Topic: " topic
    if [ -z "$topic" ]; then
        break
    fi
    topics+=("$topic")
done

if [ ${#topics[@]} -eq 0 ]; then
    echo "No topics entered. Cancelled."
    exit 0
fi

echo ""
echo "Will subscribe to:"
for topic in "${topics[@]}"; do
    echo "  - $topic"
done
echo ""

read -p "Confirm? (y/n): " confirm
if [ "$confirm" != "y" ]; then
    echo "Cancelled."
    exit 0
fi

# Update config.json
echo "Updating config.json..."
python3 << EOF
import json

with open("$CONFIG_FILE", 'r') as f:
    config = json.load(f)

config['bridge']['topics'] = [$(printf '"%s",' "${topics[@]}" | sed 's/,$//')]

with open("$CONFIG_FILE", 'w') as f:
    json.dump(config, f, indent=2)
EOF

# Update mosquitto.conf
echo "Updating mosquitto.conf..."
# Remove old topic lines
sed -i '/^topic msh/d' "$MOSQUITTO_CONF"

# Add new topics before the cleansession line
for topic in "${topics[@]}"; do
    sed -i "/^connection meshtastic-bridge/a topic $topic both 0" "$MOSQUITTO_CONF"
done

# Restart mosquitto
echo "Restarting Mosquitto..."
systemctl restart mosquitto

sleep 2

if systemctl is-active --quiet mosquitto; then
    echo ""
    echo "✓ Topics updated successfully!"
    echo ""
    echo "Your bridge is now subscribed to:"
    for topic in "${topics[@]}"; do
        echo "  - $topic"
    done
else
    echo ""
    echo "✗ Failed to restart Mosquitto"
    echo "Check logs: journalctl -u mosquitto -n 50"
    exit 1
fi
