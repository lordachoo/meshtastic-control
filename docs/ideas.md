# Meshtastic Dashboard - Feature Ideas

## High Priority Features

### 1. Auto-Responder System ⭐ IN PROGRESS
- Automatically respond to incoming messages based on configurable rules
- **Trigger types:**
  - Exact match (e.g., "ping" → "pong")
  - Contains keyword (e.g., message contains "test" → respond)
  - Regex pattern matching
  - Broadcast vs direct message filtering
- **Response types:**
  - Static text response
  - Dynamic responses with variables (node info, time, battery, etc.)
  - Multiple responses (random selection)
- **Configuration:**
  - Enable/disable individual rules
  - Set response delay (to avoid spam)
  - Limit responses per hour/day
  - Only respond to broadcasts, only to direct messages, or both
- **Use cases:**
  - Auto-reply to "ping" with "pong"
  - Respond to "test" with device info
  - Auto-reply with location/status
  - Away message for direct messages
  - Network health check responses

### 2. Map View
- Visual representation of node locations on an interactive map
- Use Leaflet.js or similar mapping library
- Show real-time movement of nodes
- Display distance/bearing calculations between nodes
- Subscribe to `meshtastic.receive.position` packets

### 2. Telemetry Dashboard
- Battery levels with historical graphs
- Signal quality graphs over time
- Device metrics (voltage, channel utilization, air utilization)
- Environment sensors (temperature, humidity, pressure if available)
- Power metrics (battery %, voltage, current)

### 3. Configuration Editor
- Read/write `localConfig` (radio settings like LoRa region, transmit power, etc.)
- Read/write `moduleConfig` (module-specific settings)
- Channel management (view/edit/add channels)
- Remote node configuration
- Change radio settings without CLI

## Medium Priority Features

### 4. Neighbor Graph / Mesh Topology Visualization
- View which nodes can directly hear each other
- Signal quality between neighbors
- Visual graph of mesh connections
- Identify mesh bottlenecks or weak links

### 5. Message Acknowledgments
- See if messages were received
- Delivery status indicators
- Message read receipts

### 6. Admin Controls
- Reboot remote nodes
- Factory reset
- Shutdown nodes
- Request position updates
- Request device metrics

### 7. User Info Tracking
- Subscribe to `meshtastic.receive.user` packets
- Show when users update their long/short names
- Display hardware model changes
- Track role changes (client/router/repeater)

## Lower Priority Features

### 8. Waypoints
- Send/receive waypoint data
- Display waypoints on map
- Waypoint management interface

### 9. Store & Forward
- View stored messages (if router has store-forward enabled)
- Request message history
- Offline message delivery status

### 10. Advanced Messaging
- Binary data packets (not just text)
- Message encryption status display
- Direct messages vs broadcast filtering
- File transfer capability

### 11. Detection Sensor Module
- Display alerts from detection sensors
- Sensor event log
- Notification system for sensor triggers

### 12. Statistics & Analytics
- Network health metrics
- Message delivery success rates
- Node uptime tracking
- Historical data analysis
- Export data to CSV/JSON

### 13. Alerts & Notifications
- Browser notifications for important messages
- Custom alert rules (e.g., low battery warnings)
- Sound alerts for specific events

### 14. Multi-Device Support
- Connect to multiple Meshtastic devices
- Switch between devices
- Aggregate view of multiple meshes

## UI/UX Improvements

- Dark/light theme toggle
- Customizable dashboard layout
- Favorite nodes quick access
- Search/filter improvements
- Keyboard shortcuts for common actions
- Mobile-responsive design
- Export/import settings
