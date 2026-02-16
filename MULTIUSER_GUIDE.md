# Multi-User Session-Based Architecture Guide

## Overview

The `app_multiuser.py` file provides a session-based architecture that allows multiple users to connect to different Meshtastic devices simultaneously. Each user gets their own session with isolated state.

## Key Changes from Single-User Version

### 1. Session Storage

**Before (Single User):**
```python
iface = None  # Global connection
packet_log = []  # Global packet log
traceroute_results = {}  # Global traceroute data
```

**After (Multi-User):**
```python
sessions = {}  # session_id -> session_data

def create_session_data():
    return {
        "iface": None,
        "packet_log": [],
        "traceroute_results": {},
        "sweep_status": {...},
        "mqtt_health": {...},
        "last_activity": time.time(),
        "node_ip": None
    }
```

### 2. Session Management Functions

```python
def get_session_id():
    """Get or create session ID from Flask session cookie."""
    if 'session_id' not in session:
        session['session_id'] = secrets.token_hex(16)
        session.permanent = True
    return session['session_id']

def get_session_data():
    """Get session data for current user, creating if needed."""
    session_id = get_session_id()
    
    with sessions_lock:
        if session_id not in sessions:
            sessions[session_id] = create_session_data()
        
        sessions[session_id]["last_activity"] = time.time()
        return sessions[session_id]
```

### 3. Automatic Session Cleanup

- Sessions timeout after 1 hour of inactivity
- Background thread runs every 5 minutes to clean up old sessions
- Maximum 10 concurrent sessions (configurable)
- Connections are properly closed when sessions expire

### 4. Route Pattern

**Before:**
```python
@app.route("/api/nodes")
def get_nodes():
    global iface
    i = iface
    if not i:
        return jsonify({"error": "Not connected"}), 400
    # ... use global state
```

**After:**
```python
@app.route("/api/nodes")
def get_nodes():
    session_data = get_session_data()
    i = session_data["iface"]
    if not i:
        return jsonify({"error": "Not connected"}), 400
    # ... use session_data instead of globals
```

### 5. Helper Function Pattern

**Before:**
```python
def get_interface():
    global iface
    return iface

def get_my_node():
    i = get_interface()
    # ...
```

**After:**
```python
def get_interface(session_data):
    return session_data["iface"]

def get_my_node(session_data):
    i = session_data["iface"]
    # ...
```

## Routes That Need Updating

All routes need to be updated to use `get_session_data()` instead of global state:

### Connection Routes
- `/api/connect` - Create connection in session_data["iface"]
- `/api/disconnect` - Close connection from session_data["iface"]
- `/api/status` - Check session_data["iface"] status

### Node Routes
- `/api/nodes` - Use session_data["iface"].nodes
- `/api/device/config` - Use session_data["iface"].localNode

### Traceroute Routes
- `/api/traceroute` - Store in session_data["traceroute_results"]
- `/api/traceroute/results` - Read from session_data["traceroute_results"]
- `/api/sweep/*` - Use session_data["sweep_status"]

### Packet Routes
- `/api/packets` - Read from session_data["packet_log"]
- `/api/send` - Send via session_data["iface"]

### MQTT Routes
- `/api/mqtt/health` - Use session_data["mqtt_health"]
- `/api/device/mqtt/config` - Use session_data["iface"]

### Device Control Routes
- `/api/device/reboot` - Use session_data["iface"]
- `/api/device/shutdown` - Use session_data["iface"]
- `/api/device/factory-reset` - Use session_data["iface"]

## Packet Handler Challenge

The `on_receive` callback needs special handling because it's called by pubsub and needs to route packets to the correct session:

**Solution:** Store session_id with the interface when connecting:

```python
@app.route("/api/connect", methods=["POST"])
def connect():
    session_data = get_session_data()
    session_id = get_session_id()
    
    # ... create interface ...
    
    # Store session_id with interface for packet routing
    session_data["iface"]._session_id = session_id
    
    # Subscribe with session-aware callback
    pub.subscribe(lambda packet, interface: on_receive(packet, interface, session_id), 
                  "meshtastic.receive")
```

Then in `on_receive`:

```python
def on_receive(packet, interface, session_id):
    """Called on every received packet - routes to correct session."""
    with sessions_lock:
        if session_id not in sessions:
            return
        session_data = sessions[session_id]
    
    # Apply message filter
    if should_filter_message(packet):
        return
    
    # Add to session's packet log
    entry = {...}
    session_data["packet_log"].append(entry)
    
    # Keep log size limited
    if len(session_data["packet_log"]) > PACKET_LOG_MAX:
        session_data["packet_log"].pop(0)
    
    # Update MQTT health for this session
    # ... update session_data["mqtt_health"] ...
    
    # Check auto-responder for this session
    response = check_autoresponder(message_text, from_id, to_id, session_data)
    if response:
        # Send via this session's interface
        session_data["iface"].sendText(response, destinationId=from_id)
```

## Testing Multi-User

1. **Start the multiuser version:**
   ```bash
   python app_multiuser.py
   ```

2. **Open two browser windows:**
   - Window 1: http://localhost:5000
   - Window 2: http://localhost:5000 (incognito/private mode for separate session)

3. **Connect to different devices:**
   - Window 1: Connect to 192.168.1.100
   - Window 2: Connect to 192.168.1.101

4. **Verify isolation:**
   - Each window should show only its own node's data
   - Packet logs should be independent
   - Traceroutes should not interfere

## Configuration

Adjust these constants in `app_multiuser.py`:

```python
SESSION_TIMEOUT = 3600  # 1 hour idle timeout
MAX_SESSIONS = 10  # Maximum concurrent users
PACKET_LOG_MAX = 500  # Packets per session
```

## Security Considerations

For production deployment:

1. **Add authentication** - Don't let random users connect to your nodes
2. **Use HTTPS** - Protect session cookies
3. **Set SECRET_KEY** - Use environment variable, not random on each restart
4. **Add rate limiting** - Prevent abuse
5. **Log access** - Track who connects to what
6. **Add IP restrictions** - Limit which devices can be connected to

## Migration Path

1. **Test side-by-side:**
   - Keep `app.py` running on port 5000
   - Run `app_multiuser.py` on port 5001 for testing
   - Compare behavior

2. **Complete the refactor:**
   - Update all routes systematically
   - Test each route after updating
   - Update pubsub callbacks last

3. **Switch over:**
   - When ready, rename `app.py` to `app_single.py` (backup)
   - Rename `app_multiuser.py` to `app.py`
   - Restart service

## Current Status

✅ Session management framework complete
✅ Helper functions updated
✅ Auto-responder session-aware
⚠️ Routes need systematic updating (26 routes)
⚠️ Pubsub callbacks need session routing
⚠️ Frontend works as-is (session cookies automatic)

## Next Steps

To complete the refactor:

1. Update `/api/connect` route to use `get_session_data()`
2. Update `/api/disconnect` route
3. Update all `/api/nodes*` routes
4. Update all `/api/traceroute*` routes
5. Update all `/api/packets*` and `/api/send` routes
6. Update all device control routes
7. Update `on_receive` callback with session routing
8. Test thoroughly with multiple concurrent users

The foundation is in place - now it's systematic route-by-route updates!
