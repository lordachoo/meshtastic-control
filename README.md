# Meshtastic Dashboard

A self-contained web dashboard for managing your Meshtastic node and testing RF reach.

Connects to your Meshtastic device over TCP (WiFi) and provides:

- **Node Overview** — See all nodes in your mesh with filtering (RF-only, MQTT, nearby)
- **RF Traceroute** — Test signal reach to individual nodes and see the hop path
- **RF Sweep** — Automated traceroute to all RF-reachable nodes to map your coverage
- **Live Packet Log** — Watch packets flowing through the mesh in real-time
- **Send Messages** — Broadcast or direct-message nodes from the web UI

## Requirements

- Python 3.8+
- A Meshtastic device with WiFi enabled and network API accessible (TCP port 4403)

## Setup

```bash
# Install dependencies
pip install meshtastic flask

# Run the dashboard
python app.py
```

Then open **http://localhost:5000** in your browser.

## Usage

1. Enter your Meshtastic node's IP address (e.g., `192.168.1.134`) and click **Connect**
2. The dashboard will pull your node database and display all known nodes
3. Use the **Nodes** tab to browse the mesh — filter by RF/MQTT/Nearby
4. Use the **RF Traceroute** tab to:
   - Send individual traceroutes to specific nodes
   - Run an **RF Sweep** to automatically test all reachable nodes
5. Use the **Packet Log** tab to watch live traffic and send messages

## Node Filters

| Filter | Description |
|--------|-------------|
| **All** | Every node your device knows about |
| **RF Only** | Nodes heard directly over LoRa (not via MQTT internet relay) |
| **MQTT** | Nodes only reachable via the MQTT internet bridge |
| **Nearby** | Non-MQTT nodes within 2 hops |

## RF Sweep

The sweep feature automatically sends traceroutes to all non-MQTT nodes (and any MQTT nodes within 2 hops). Each node gets a 30-second timeout, with a 3-second delay between tests to avoid overwhelming the mesh. Results show:

- **Complete** — Node responded with route path and SNR data
- **Timeout** — Node did not respond within 30 seconds
- **Error** — Something went wrong sending the traceroute

## Notes

- Your Meshtastic device must have WiFi enabled and be on the same network
- The default TCP API port is 4403
- The dashboard runs on port 5000 by default
- Only one client should connect to the Meshtastic TCP API at a time
- RF Sweep can take a long time if you have many non-MQTT nodes — each gets up to 30 seconds

## Troubleshooting MQTT with mosquitto_sub

You can use the `mosquitto_sub` command-line client to monitor MQTT traffic directly and troubleshoot connectivity issues. This is useful for:

- Verifying your device is publishing to MQTT
- Seeing your own messages and responses
- Testing different topic subscriptions
- Debugging auto-responder behavior

### Installation

**Linux/macOS:**
```bash
# Ubuntu/Debian
sudo apt-get install mosquitto-clients

# macOS
brew install mosquitto
```

**Windows:**
Download from [mosquitto.org](https://mosquitto.org/download/) or use WSL2.

### Basic Usage

Monitor all traffic for your region:
```bash
mosquitto_sub -h mqtt.meshtastic.org -u meshdev -P large4cats -t "msh/US/#" -v -i YOUR_CALLSIGN_monitor
```

Monitor specific state (e.g., California):
```bash
mosquitto_sub -h mqtt.meshtastic.org -u meshdev -P large4cats -t "msh/US/CA/#" -v -i YOUR_CALLSIGN_monitor
```

Monitor only your node's traffic:
```bash
mosquitto_sub -h mqtt.meshtastic.org -u meshdev -P large4cats -t "msh/2/e/!YOUR_NODE_ID/#" -v -i YOUR_CALLSIGN_monitor
```

### Command Options

| Option | Description |
|--------|-------------|
| `-h` | MQTT broker hostname |
| `-u` | Username (default: `meshdev`) |
| `-P` | Password (default: `large4cats`) |
| `-t` | Topic to subscribe to (use `#` wildcard for all subtopics) |
| `-v` | Verbose mode (shows topic name with each message) |
| `-i` | Client ID (use your callsign + `_monitor` to avoid conflicts) |

### Example Output

```
msh/US/CA/2/e/LongFast/!02ed4dfc {"from":34680060,"to":4294967295,"channel":0,"type":"text","payload":"test"}
msh/US/CA/2/e/LongFast/!040944e0 {"from":67634400,"to":4294967295,"channel":0,"type":"position","latitude":37.7749,"longitude":-122.4194}
```

### Tips

- **Use `-v` flag** to see which topic each message came from
- **Set unique client ID** with `-i` to avoid disconnections (use your callsign)
- **Start with narrow topics** like your state or node ID to reduce noise
- **Test auto-responder** by sending "ping" or "test" and watching for replies
- **Monitor while using dashboard** to see both sides of the conversation

### Common Topics

| Topic Pattern | What You'll See |
|---------------|-----------------|
| `msh/US/#` | All US traffic (very high volume) |
| `msh/US/CA/#` | California only |
| `msh/2/e/LongFast/#` | Default encrypted channel (all regions) |
| `msh/2/e/!YOUR_NODE_ID/#` | Only your node's messages |
| `msh/2/c/#` | Unencrypted messages only |

## Architecture

```
Browser  <-->  Flask (port 5000)  <-->  Meshtastic Python API  <-->  Your Node (TCP:4403)
```

Single Python process, no database needed. All state is held in memory.
