# Meshtastic MQTT Channel Reference

This document lists common MQTT topic patterns used in the Meshtastic ecosystem.

## Topic Structure

Meshtastic MQTT topics follow this pattern:
```
msh/<region>/<version>/<encryption>/<channel_or_node>
```

## Common Patterns

### By Region
- `msh/US/#` - All United States traffic
- `msh/EU/#` - All European traffic
- `msh/ANZ/#` - Australia/New Zealand
- `msh/TW/#` - Taiwan
- `msh/JP/#` - Japan

### By Encryption/Channel Type
- `msh/2/e/#` - Encrypted messages (version 2)
- `msh/2/c/#` - Unencrypted channel messages
- `msh/2/json/#` - JSON formatted messages

### By Channel Name
Common default channels:
- `msh/US/2/e/LongFast` - Default encrypted channel (US)
- `msh/US/2/c/LongFast` - Default unencrypted channel (US)
- `msh/2/e/LongFast/#` - LongFast channel (all regions)

### By Node ID
- `msh/2/e/!YOUR_NODE_ID/#` - Messages to/from specific node
- `msh/US/2/e/!02ed4dfc/#` - Specific node in US region

## Traffic Levels

⚠️ **Very High Traffic** (thousands of messages/hour):
- `msh/US/#`
- `msh/2/e/#`

⚠️ **High Traffic** (hundreds of messages/hour):
- `msh/US/2/e/LongFast`
- `msh/2/e/LongFast/#`

✅ **Low Traffic** (your messages only):
- `msh/2/e/!YOUR_NODE_ID/#`
- `msh/US/2/e/!YOUR_NODE_ID/#`

## Finding Your Channel

To find what channel your device is using:

1. **Via Dashboard:**
   - Go to Device Settings tab
   - Look at "Channels" section
   - Note the channel name (usually "LongFast" or custom)

2. **Via CLI:**
   ```bash
   meshtastic --host <device-ip> --info
   ```

3. **Via MQTT Config:**
   - Check your device's `mqtt.root` setting
   - This is what you're currently subscribed to

## Recommended Settings

**For minimal traffic (recommended):**
```
mqtt.root = msh/2/e/!YOUR_NODE_ID
```

**For specific channel:**
```
mqtt.root = msh/US/2/e/YourChannelName
```

**For region-wide (heavy traffic):**
```
mqtt.root = msh/US
```

## Custom Channels

You can create custom channels by:
1. Setting a custom channel name in your device settings
2. Using that name in the MQTT topic pattern
3. Only devices with the same channel name and encryption key will see messages

Example: If you create a channel called "MyTeam":
- Topic: `msh/US/2/e/MyTeam`
- Only devices with "MyTeam" channel and matching encryption key will receive

## Notes

- The public MQTT broker (`mqtt.meshtastic.org`) doesn't provide a directory of active channels
- You can only discover channels by monitoring traffic or knowing the channel names
- Most traffic is on default channels like "LongFast"
- Custom channels are private and not discoverable unless you know the name
