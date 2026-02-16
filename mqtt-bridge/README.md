# Meshtastic MQTT Bridge

This directory contains scripts and configuration for running a local MQTT broker that bridges to the public `mqtt.meshtastic.org` server.

## What This Does

Your local Mosquitto broker will:
- **Bridge to** the public Meshtastic MQTT server
- **Serve your local devices** with better performance and reliability
- **Sync all public mesh traffic** to your local broker
- **Allow monitoring** of local vs bridged traffic
- **Continue working** even if the public server is down

## Architecture

```
Your Meshtastic Devices → Local Mosquitto Broker → mqtt.meshtastic.org (public mesh)
                          (localhost:1883)
```

## Installation

### 1. Run Setup Script

```bash
cd mqtt-bridge
sudo ./setup.sh
```

This will:
- Install Mosquitto MQTT broker
- Configure bridge to mqtt.meshtastic.org
- Set up authentication (username/password from config.json)
- Create and enable systemd service
- Start the broker

### 2. Configure Your Meshtastic Devices

Update MQTT settings on each device:

**Via Meshtastic App:**
1. Go to Settings → MQTT
2. Enable MQTT
3. Set Server: `<your-server-ip>` (or `localhost` if on same machine)
4. Set Port: `1883`
5. Set Username: `meshtastic` (or your custom username)
6. Set Password: `meshtastic123` (or your custom password)
7. Disable TLS/Encryption (local network)
8. Save and reboot device

**Via CLI:**
```bash
meshtastic --host <device-ip> --set mqtt.enabled true
meshtastic --host <device-ip> --set mqtt.address <your-server-ip>
meshtastic --host <device-ip> --set mqtt.username meshtastic
meshtastic --host <device-ip> --set mqtt.password meshtastic123
```

## Configuration

### Default Credentials

Edit `config.json` to change default credentials:

```json
{
  "broker": {
    "username": "meshtastic",
    "password": "meshtastic123"
  }
}
```

### Update Credentials

To change username/password after installation:

```bash
cd mqtt-bridge
sudo ./update-config.sh
```

### Bridged Topics

By default, the broker subscribes to a minimal topic set to reduce traffic:
- `msh/2/e/!YOUR_NODE_ID/#` - Only messages to/from your specific node

**Common topic patterns:**
- `msh/US/#` - All US region traffic (⚠️ LOTS of data)
- `msh/2/e/#` - All encrypted messages (⚠️ Still a lot)
- `msh/2/e/!YOUR_NODE_ID/#` - Only your node's messages (✅ Recommended)
- `msh/2/c/LongFast/#` - Specific channel name
- `msh/2/json/#` - JSON formatted messages only

**To customize topics:**

```bash
cd mqtt-bridge
sudo ./update-topics.sh
```

This script will:
1. Show current subscribed topics
2. Let you enter new topics interactively
3. Update both `config.json` and `mosquitto.conf`
4. Restart the broker

**Manual configuration:**

Edit `config.json` and update the `topics` array:
```json
"topics": [
  "msh/2/e/!02ed4dfc/#",
  "msh/2/c/MyChannel/#"
]
```

Then run `sudo ./setup.sh` again to apply changes.

## Management

### Check Status

```bash
sudo systemctl status mosquitto
```

### View Logs

```bash
sudo journalctl -u mosquitto -f
```

Or check the log file:
```bash
sudo tail -f /var/log/mosquitto/mosquitto.log
```

### Restart Service

```bash
sudo systemctl restart mosquitto
```

### Stop Service

```bash
sudo systemctl stop mosquitto
```

## Dashboard Integration

The Meshtastic Dashboard will automatically detect if the local broker is running and offer to switch to it.

**In the dashboard:**
1. Go to Device Settings tab
2. Look for "MQTT Configuration" section
3. Toggle between Public Server / Local Bridge
4. View bridge health and connection status

## Troubleshooting

### Service won't start

Check logs:
```bash
sudo journalctl -u mosquitto -n 50
```

Common issues:
- Port 1883 already in use
- Permission issues with log/data directories
- Invalid configuration syntax

### Can't connect from devices

1. Check firewall allows port 1883:
   ```bash
   sudo ufw allow 1883/tcp
   ```

2. Verify broker is listening:
   ```bash
   sudo netstat -tlnp | grep 1883
   ```

3. Test connection:
   ```bash
   mosquitto_sub -h localhost -p 1883 -u meshtastic -P meshtastic123 -t 'msh/#' -v
   ```

### Bridge not syncing

Check bridge connection in logs:
```bash
sudo grep "meshtastic-bridge" /var/log/mosquitto/mosquitto.log
```

Should see connection attempts to mqtt.meshtastic.org.

## Files

- `config.json` - Broker configuration (username/password)
- `mosquitto.conf` - Mosquitto broker configuration
- `setup.sh` - Installation script
- `update-config.sh` - Update credentials script
- `README.md` - This file

## Security Notes

- Default credentials are **not secure** - change them!
- The broker allows local network access only
- TLS/encryption is disabled for local connections
- Bridge to public server uses unencrypted connection (standard for Meshtastic)

## Uninstall

To remove the MQTT bridge:

```bash
sudo systemctl stop mosquitto
sudo systemctl disable mosquitto
sudo apt remove mosquitto mosquitto-clients
sudo rm /etc/mosquitto/conf.d/meshtastic-bridge.conf
sudo rm /etc/mosquitto/passwd
```
