"""
Meshtastic Dashboard - Web-based node management and RF testing tool
Connects to a Meshtastic node via TCP and provides a web UI for:
- Node overview and status
- Traceroute testing (RF reach mapping)
- Automated RF sweep (traceroute all non-MQTT nodes)
- Live packet monitoring
- Sending messages
"""

import json
import time
import math
import threading
import logging
import os
import secrets
from datetime import datetime, timezone
from flask import Flask, render_template, jsonify, request, session, session
from google.protobuf.json_format import MessageToDict

import meshtastic
import meshtastic.tcp_interface
from pubsub import pub

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = 3600  # 1 hour

app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = 3600  # 1 hour

# Auto-responder configuration
AUTORESPONDER_FILE = "autoresponder.json"
autoresponder_config = {"enabled": True, "rules": []}
last_response_times = {}  # Track cooldowns per rule

# Beacon configuration
BEACON_FILE = "beacon.json"
beacon_config = {"enabled": False, "interval_minutes": 60, "message_template": "", "channel": 0}
beacon_threads = {}  # session_id -> beacon thread

# Persistent sessions configuration
PERSISTENT_SESSIONS_FILE = "persistent_sessions.json"
persistent_sessions = {}  # session_name -> session_config
persistent_sessions_lock = threading.Lock()

# ── Session-Based State ────────────────────────────────────────────────────
# Each session gets its own connection and state
sessions = {}  # session_id -> session_data
sessions_lock = threading.Lock()
PACKET_LOG_MAX = 500
SESSION_TIMEOUT = 3600  # 1 hour idle timeout
MAX_SESSIONS = 10  # Limit concurrent sessions

def create_session_data():
    """Create a new session data structure."""
    return {
        "iface": None,
        "packet_log": [],
        "traceroute_results": {},
        "sweep_status": {
            "running": False,
            "current": None,
            "total": 0,
            "completed": 0,
            "results": {}
        },
        "mqtt_health": {
            "last_mqtt_message": None,
            "last_rf_message": None,
            "mqtt_message_count": 0,
            "rf_message_count": 0,
            "mqtt_sent_count": 0,
            "connection_start": None
        },
        "last_activity": time.time(),
        "connected_at": None,
        "node_ip": None,
        "my_node_info": None  # Cache node info here
    }

def get_session_id():
    """Get or create session ID for current request."""
    if 'session_id' not in session:
        session['session_id'] = secrets.token_hex(16)
        session.permanent = True
    return session['session_id']

def get_session_data(session_name=None):
    """Get session data for current user, creating if needed.
    
    Args:
        session_name: Optional persistent session name to use instead of Flask session
    """
    # If session_name provided, use persistent session
    if session_name:
        with persistent_sessions_lock:
            if session_name not in persistent_sessions:
                raise ValueError(f"Session '{session_name}' not found")
            session_id = persistent_sessions[session_name]["session_id"]
            session_config = persistent_sessions[session_name]
        print(f"[SESSION] Looking up session '{session_name}' -> session_id={session_id[:8]}")
    else:
        # Fall back to Flask session
        session_id = get_session_id()
        session_config = None
        print(f"[SESSION] Using Flask session_id={session_id[:8]}")
    
    with sessions_lock:
        if session_id not in sessions:
            # Check session limit
            if len(sessions) >= MAX_SESSIONS:
                # Clean up oldest inactive session
                cleanup_old_sessions(force_one=True)
                
            sessions[session_id] = create_session_data()
            print(f"[SESSION] Created NEW session data for {session_id[:8]} (no existing data found)")
            
            # If this is a persistent session with connection info, auto-reconnect
            if session_config and session_config.get("host"):
                print(f"[SESSION] Auto-reconnecting persistent session '{session_name}' to {session_config['host']}:{session_config['port']}")
                try:
                    # Create interface
                    sessions[session_id]["iface"] = meshtastic.tcp_interface.TCPInterface(
                        hostname=session_config["host"],
                        portNumber=session_config["port"]
                    )
                    sessions[session_id]["iface"]._session_id = session_id
                    sessions[session_id]["mqtt_health"]["connection_start"] = time.time()
                    sessions[session_id]["node_ip"] = session_config["host"]
                    sessions[session_id]["connected_at"] = time.time()
                    
                    # Wait briefly for node DB to populate, then cache node info
                    time.sleep(2)
                    my_node = get_my_node(sessions[session_id])
                    sessions[session_id]["my_node_info"] = my_node
                    print(f"[SESSION] Auto-reconnect successful for '{session_name}', cached node: {my_node.get('id') if my_node else 'unknown'}")
                    
                    # Start beacon if enabled
                    if beacon_config.get("enabled", False):
                        print(f"[BEACON] Starting beacon thread for auto-reconnected session '{session_name}'")
                        start_beacon(session_id)
                except Exception as e:
                    print(f"[SESSION] Auto-reconnect failed for '{session_name}': {e}")
                    logger.error(f"Auto-reconnect failed: {e}")
            
            logger.info(f"Created new session: {session_id}")
        else:
            has_iface = sessions[session_id]["iface"] is not None
            print(f"[SESSION] Found EXISTING session data for {session_id[:8]}, has_iface={has_iface}")
        
        # Update last activity
        sessions[session_id]["last_activity"] = time.time()
        return sessions[session_id]

def cleanup_old_sessions(force_one=False):
    """Remove sessions that have been idle too long."""
    now = time.time()
    to_remove = []
    
    for sid, sdata in sessions.items():
        age = now - sdata["last_activity"]
        if age > SESSION_TIMEOUT or (force_one and not to_remove):
            to_remove.append(sid)
            # Close connection if open
            if sdata["iface"]:
                try:
                    sdata["iface"].close()
                except:
                    pass
    
    for sid in to_remove:
        del sessions[sid]
        logger.info(f"Cleaned up session: {sid}")
    
    return len(to_remove)

# Run cleanup periodically
def cleanup_thread():
    """Background thread to clean up old sessions."""
    while True:
        time.sleep(300)  # Check every 5 minutes
        with sessions_lock:
            cleanup_old_sessions()

cleanup_worker = threading.Thread(target=cleanup_thread, daemon=True)
cleanup_worker.start()

# Message filtering configuration
MESSAGE_FILTER_FILE = "message_filter.json"
message_filter = {
    "enabled": False,
    "filter_mode": "allowlist",  # "allowlist" or "blocklist"
    "node_ids": [],  # List of node IDs to allow/block (e.g., ["!02ed4dfc", "!040944e0"])
    "portnums": [],  # List of portnums to allow/block (e.g., ["TEXT_MESSAGE_APP", "POSITION_APP"])
}

def load_message_filter():
    """Load message filter configuration from file."""
    global message_filter
    if os.path.exists(MESSAGE_FILTER_FILE):
        try:
            with open(MESSAGE_FILTER_FILE, 'r') as f:
                message_filter = json.load(f)
                logger.info(f"Loaded message filter: {message_filter}")
        except Exception as e:
            logger.error(f"Error loading message filter: {e}")

def save_message_filter():
    """Save message filter configuration to file."""
    try:
        with open(MESSAGE_FILTER_FILE, 'w') as f:
            json.dump(message_filter, f, indent=2)
        logger.info(f"Saved message filter")
    except Exception as e:
        logger.error(f"Error saving message filter: {e}")

def should_filter_message(packet):
    """Check if a message should be filtered out based on current filter settings."""
    if not message_filter.get("enabled", False):
        return False
    
    from_id = packet.get("fromId", "")
    portnum = packet.get("decoded", {}).get("portnum", "")
    
    filter_mode = message_filter.get("filter_mode", "allowlist")
    node_ids = message_filter.get("node_ids", [])
    portnums = message_filter.get("portnums", [])
    
    # Check node ID filtering
    if node_ids:
        node_match = from_id in node_ids
        if filter_mode == "allowlist" and not node_match:
            logger.info(f"Message filter: BLOCKING {from_id} (not in allowlist)")
            return True  # Filter out (not in allowlist)
        elif filter_mode == "blocklist" and node_match:
            logger.info(f"Message filter: BLOCKING {from_id} (in blocklist)")
            return True  # Filter out (in blocklist)
    
    # Check portnum filtering
    if portnums:
        portnum_match = portnum in portnums
        if filter_mode == "allowlist" and not portnum_match:
            logger.info(f"Message filter: BLOCKING {portnum} (not in allowlist)")
            return True  # Filter out (not in allowlist)
        elif filter_mode == "blocklist" and portnum_match:
            logger.info(f"Message filter: BLOCKING {portnum} (in blocklist)")
            return True  # Filter out (in blocklist)
    
    return False  # Don't filter

load_message_filter()


# ── Persistent Session Management ──────────────────────────────────────────

def load_persistent_sessions():
    """Load persistent sessions from JSON file."""
    global persistent_sessions
    try:
        with open(PERSISTENT_SESSIONS_FILE, 'r') as f:
            persistent_sessions = json.load(f)
        logger.info(f"Loaded {len(persistent_sessions)} persistent sessions")
    except FileNotFoundError:
        persistent_sessions = {}
        logger.info("No persistent sessions file found, starting fresh")
    except Exception as e:
        logger.error(f"Error loading persistent sessions: {e}")
        persistent_sessions = {}


def save_persistent_sessions():
    """Save persistent sessions to JSON file."""
    try:
        with open(PERSISTENT_SESSIONS_FILE, 'w') as f:
            json.dump(persistent_sessions, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving persistent sessions: {e}")


def create_persistent_session(name, host, port, auto_connect=False):
    """Create a new persistent session configuration."""
    with persistent_sessions_lock:
        if name in persistent_sessions:
            return {"error": "Session name already exists"}
        
        session_id = secrets.token_hex(16)
        persistent_sessions[name] = {
            "session_id": session_id,
            "name": name,
            "host": host,
            "port": port,
            "auto_connect": auto_connect,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_connected": None,
            "beacon_enabled": False,
            "beacon_interval": 60,
            "beacon_message": "",
            "beacon_channel": 0,
            "autoresponder_enabled": False,
            "autoresponder_rules": []
        }
        save_persistent_sessions()
        
        # Create runtime session data
        with sessions_lock:
            if session_id not in sessions:
                sessions[session_id] = create_session_data()
        
        return {"success": True, "session_id": session_id, "name": name}


def delete_persistent_session(name):
    """Delete a persistent session."""
    with persistent_sessions_lock:
        if name not in persistent_sessions:
            return {"error": "Session not found"}
        
        session_config = persistent_sessions[name]
        session_id = session_config["session_id"]
        
        # Disconnect if connected
        with sessions_lock:
            if session_id in sessions:
                session_data = sessions[session_id]
                if session_data["iface"]:
                    try:
                        session_data["iface"].close()
                    except:
                        pass
                # Stop beacon if running
                stop_beacon(session_id)
                del sessions[session_id]
        
        del persistent_sessions[name]
        save_persistent_sessions()
        return {"success": True}


def get_session_by_name(name):
    """Get session ID and data by session name."""
    with persistent_sessions_lock:
        if name not in persistent_sessions:
            return None, None
        session_config = persistent_sessions[name]
        session_id = session_config["session_id"]
    
    with sessions_lock:
        session_data = sessions.get(session_id)
    
    return session_id, session_data


def update_persistent_session_config(name, updates):
    """Update persistent session configuration."""
    with persistent_sessions_lock:
        if name not in persistent_sessions:
            return {"error": "Session not found"}
        
        persistent_sessions[name].update(updates)
        save_persistent_sessions()
        return {"success": True}


# ── Auto-Responder Functions ────────────────────────────────────────────────

def load_autoresponder_config():
    """Load auto-responder configuration from JSON file."""
    global autoresponder_config
    try:
        if os.path.exists(AUTORESPONDER_FILE):
            with open(AUTORESPONDER_FILE, 'r') as f:
                autoresponder_config = json.load(f)
                logger.info(f"Loaded {len(autoresponder_config.get('rules', []))} auto-responder rules")
        else:
            save_autoresponder_config()
    except Exception as e:
        logger.error(f"Error loading auto-responder config: {e}")


def save_autoresponder_config():
    """Save auto-responder configuration to JSON file."""
    try:
        with open(AUTORESPONDER_FILE, 'w') as f:
            json.dump(autoresponder_config, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving auto-responder config: {e}")


def load_beacon_config():
    """Load beacon configuration from JSON file."""
    global beacon_config
    try:
        if os.path.exists(BEACON_FILE):
            with open(BEACON_FILE, 'r') as f:
                beacon_config = json.load(f)
                logger.info(f"Loaded beacon config: interval={beacon_config.get('interval_minutes')}min")
        else:
            save_beacon_config()
    except Exception as e:
        logger.error(f"Error loading beacon config: {e}")


def save_beacon_config():
    """Save beacon configuration to JSON file."""
    try:
        with open(BEACON_FILE, 'w') as f:
            json.dump(beacon_config, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving beacon config: {e}")


def substitute_beacon_variables(template, session_data):
    """Replace variables in beacon template with actual values."""
    i = session_data["iface"]
    if not i:
        return template
    
    my_node = get_my_node(session_data)
    my_name = my_node.get("longName", "Node") if my_node else "Node"
    my_id = my_node.get("id", "!unknown") if my_node else "!unknown"
    
    # Get device metrics
    battery = "?"
    voltage = "?"
    temp = "?"
    uptime = "?"
    lat = "?"
    lon = "?"
    alt = "?"
    
    try:
        # Try to get metrics from myInfo/localNode
        if hasattr(i, 'myInfo') and i.myInfo:
            my_node_num = i.myInfo.my_node_num
            if my_node_num and my_node_num in i.nodesByNum:
                node = i.nodesByNum[my_node_num]
                
                # Get device metrics from telemetry
                if 'deviceMetrics' in node:
                    metrics = node['deviceMetrics']
                    battery = str(metrics.get('batteryLevel', '?'))
                    voltage = str(metrics.get('voltage', '?'))
                    uptime_sec = metrics.get('uptimeSeconds', 0)
                    if uptime_sec:
                        hours = uptime_sec // 3600
                        minutes = (uptime_sec % 3600) // 60
                        uptime = f"{hours}h{minutes}m"
                
                # Get position
                if 'position' in node:
                    pos = node['position']
                    lat_raw = pos.get('latitude', 0)
                    lon_raw = pos.get('longitude', 0)
                    alt_raw = pos.get('altitude', 0)
                    if lat_raw:
                        lat = f"{lat_raw:.6f}"
                    if lon_raw:
                        lon = f"{lon_raw:.6f}"
                    if alt_raw:
                        alt = str(int(alt_raw))
    except Exception as e:
        logger.warning(f"Error getting beacon metrics: {e}")
    
    # Variable substitutions
    variables = {
        "{my_name}": my_name,
        "{my_id}": my_id,
        "{battery}": battery,
        "{voltage}": voltage,
        "{temp}": temp,
        "{uptime}": uptime,
        "{time}": datetime.now().strftime("%H:%M:%S"),
        "{lat}": lat,
        "{lon}": lon,
        "{alt}": alt
    }
    
    result = template
    for var, value in variables.items():
        result = result.replace(var, str(value))
    
    return result


def beacon_loop(session_id):
    """Background thread that sends beacon messages at configured intervals."""
    print(f"[BEACON] Beacon thread started for session {session_id[:8]}")
    logger.info(f"Beacon thread started for session {session_id[:8]}")
    
    while True:
        try:
            # Check if beacon is still enabled
            if not beacon_config.get("enabled", False):
                logger.info(f"Beacon disabled, stopping thread for session {session_id[:8]}")
                break
            
            # Get session data
            with sessions_lock:
                if session_id not in sessions:
                    logger.info(f"Session {session_id[:8]} no longer exists, stopping beacon")
                    break
                session_data = sessions[session_id]
            
            # Check if interface is still connected
            if not session_data["iface"]:
                logger.info(f"Interface disconnected for session {session_id[:8]}, stopping beacon")
                break
            
            # Sleep FIRST, then send beacon (prevents spam on reconnect)
            interval_minutes = beacon_config.get("interval_minutes", 60)
            sleep_seconds = interval_minutes * 60
            
            print(f"[BEACON] Sleeping for {interval_minutes} minutes before sending beacon")
            logger.info(f"Beacon thread sleeping for {interval_minutes} minutes ({sleep_seconds}s) for session {session_id[:8]}")
            
            # Sleep in small chunks to allow for quick shutdown
            # Log countdown every 60 seconds
            for i in range(sleep_seconds):
                if not beacon_config.get("enabled", False):
                    logger.info(f"Beacon disabled during sleep, exiting thread for session {session_id[:8]}")
                    break
                
                # Log countdown every minute
                remaining = sleep_seconds - i
                if remaining % 60 == 0 and remaining > 0:
                    mins_remaining = remaining // 60
                    print(f"[BEACON] Countdown: {mins_remaining} minute(s) until next beacon (session {session_id[:8]})")
                    logger.info(f"Beacon countdown: {mins_remaining} minute(s) until next beacon (session {session_id[:8]})")
                
                time.sleep(1)
            
            # Check again if still enabled after sleep
            if not beacon_config.get("enabled", False):
                break
                
            # Check if interface still connected after sleep
            if not session_data["iface"]:
                logger.info(f"Interface disconnected during sleep for session {session_id[:8]}, stopping beacon")
                break
            
            # NOW send beacon message after the wait
            template = beacon_config.get("message_template", "")
            if template:
                message = substitute_beacon_variables(template, session_data)
                channel = beacon_config.get("channel", 0)
                
                try:
                    session_data["iface"].sendText(message, channelIndex=channel)
                    print(f"[BEACON] Sent beacon from session {session_id[:8]}: {message}")
                    logger.info(f"Beacon sent from session {session_id[:8]}: {message}")
                    
                    # Add to packet log
                    my_node = get_my_node(session_data)
                    my_id = my_node["id"] if my_node else "local"
                    
                    with sessions_lock:
                        session_data["packet_log"].append({
                            "time": datetime.now(timezone.utc).isoformat(),
                            "from": my_id,
                            "to": "^all",
                            "portnum": "TEXT_MESSAGE_APP",
                            "text": message,
                            "snr": None,
                            "rssi": None,
                            "hopStart": None,
                            "hopLimit": None,
                            "outgoing": True
                        })
                        
                        if len(session_data["packet_log"]) > PACKET_LOG_MAX:
                            session_data["packet_log"].pop(0)
                    
                    # Update last beacon time
                    beacon_config["last_beacon_time"] = datetime.now(timezone.utc).isoformat()
                    save_beacon_config()
                    
                except Exception as e:
                    logger.error(f"Error sending beacon: {e}")
                
        except Exception as e:
            logger.error(f"Error in beacon loop: {e}")
            break
    
    # Clean up
    with sessions_lock:
        if session_id in beacon_threads:
            del beacon_threads[session_id]
    logger.info(f"Beacon thread stopped for session {session_id[:8]}")


def start_beacon(session_id):
    """Start beacon thread for a session."""
    global beacon_threads
    
    print(f"[BEACON] start_beacon() called for session {session_id[:8]}")
    
    # Stop existing beacon if running
    stop_beacon(session_id)
    
    # Start new beacon thread
    print(f"[BEACON] Creating and starting beacon thread...")
    thread = threading.Thread(target=beacon_loop, args=(session_id,), daemon=True)
    thread.start()
    beacon_threads[session_id] = thread
    print(f"[BEACON] Started beacon thread for session {session_id[:8]}")
    logger.info(f"Started beacon for session {session_id[:8]}")


def stop_beacon(session_id):
    """Stop beacon thread for a session."""
    if session_id in beacon_threads:
        # Thread will stop itself when it checks enabled flag
        logger.info(f"Stopping beacon for session {session_id[:8]}")
        # Thread is daemon and will check enabled flag on next iteration


def substitute_variables(response_text, from_id, to_id, message_text, my_node, session_data):
    """Replace variables in response text with actual values."""
    i = session_data["iface"]
    
    # Get sender node info
    sender_node = None
    if i and i.nodes:
        for node_id, node in i.nodes.items():
            if node.get("user", {}).get("id") == from_id:
                sender_node = node
                break
    
    sender_name = sender_node.get("user", {}).get("longName", "Unknown") if sender_node else "Unknown"
    sender_short = sender_node.get("user", {}).get("shortName", "???") if sender_node else "???"
    
    my_name = my_node.get("longName", "Node") if my_node else "Node"
    my_id = my_node.get("id", "!unknown") if my_node else "!unknown"
    
    # Variable substitutions
    variables = {
        "{sender_name}": sender_name,
        "{sender_short}": sender_short,
        "{sender_id}": from_id,
        "{my_name}": my_name,
        "{my_id}": my_id,
        "{message}": message_text,
        "{message_type}": "broadcast" if to_id == "^all" else "direct"
    }
    
    result = response_text
    for var, value in variables.items():
        result = result.replace(var, str(value))
    
    return result


def check_autoresponder(message_text, from_id, to_id, session_data):
    """Check if message should trigger an auto-response."""
    global last_response_times
    
    print(f"[AUTO-RESPONDER] check_autoresponder() called: enabled={autoresponder_config.get('enabled')}, rules={len(autoresponder_config.get('rules', []))}")
    
    if not autoresponder_config.get("enabled", False):
        print(f"[AUTO-RESPONDER] ✗ Auto-responder is DISABLED")
        logger.info(f"Auto-responder disabled")
        return None
    
    # Skip null or empty messages
    if not message_text or message_text.strip() == "":
        logger.info(f"Empty message, skipping auto-responder")
        return None
    
    # Don't respond to our own messages
    my_node = get_my_node(session_data)
    if my_node and from_id == my_node["id"]:
        print(f"[AUTO-RESPONDER] ✗ Message from self ({from_id}), skipping")
        logger.info(f"Message from self ({from_id}), skipping auto-responder")
        return None
    
    print(f"[AUTO-RESPONDER] Processing message '{message_text}' from {from_id} (my_node={my_node['id'] if my_node else 'None'})")
    logger.info(f"Auto-responder checking message '{message_text}' from {from_id} (my_node={my_node['id'] if my_node else 'None'})")
    
    is_broadcast = to_id == "^all"
    message_lower = message_text.lower().strip()
    
    print(f"[AUTO-RESPONDER] Checking {len(autoresponder_config.get('rules', []))} rules against message '{message_lower}'")
    
    for rule in autoresponder_config.get("rules", []):
        rule_id = rule.get("id", "unknown")
        
        if not rule.get("enabled", False):
            print(f"[AUTO-RESPONDER] Rule '{rule_id}' is disabled, skipping")
            logger.info(f"Rule '{rule_id}' is disabled, skipping")
            continue
        
        # Check message type filter
        msg_type = rule.get("messageType", "both")
        if msg_type == "broadcast" and not is_broadcast:
            print(f"[AUTO-RESPONDER] Rule '{rule_id}' requires broadcast, message is direct, skipping")
            logger.info(f"Rule '{rule_id}' requires broadcast, message is direct, skipping")
            continue
        if msg_type == "direct" and is_broadcast:
            print(f"[AUTO-RESPONDER] Rule '{rule_id}' requires direct, message is broadcast, skipping")
            logger.info(f"Rule '{rule_id}' requires direct, message is broadcast, skipping")
            continue
        
        # Check trigger
        trigger = rule.get("trigger", "").lower()
        trigger_type = rule.get("triggerType", "exact")
        
        matched = False
        if trigger_type == "exact":
            matched = message_lower == trigger
        elif trigger_type == "contains":
            matched = trigger in message_lower
        elif trigger_type == "startswith":
            matched = message_lower.startswith(trigger)
        
        print(f"[AUTO-RESPONDER] Rule '{rule_id}': trigger='{trigger}', type={trigger_type}, matched={matched}")
        logger.info(f"Rule '{rule_id}': trigger='{trigger}', type={trigger_type}, message='{message_lower}', matched={matched}")
        
        if not matched:
            continue
        
        # Check cooldown
        cooldown = rule.get("cooldownSeconds", 60)
        now = time.time()
        last_time = last_response_times.get(rule_id, 0)
        time_since_last = now - last_time
        
        logger.info(f"Rule '{rule_id}' matched! Cooldown: {cooldown}s, time since last: {time_since_last:.1f}s")
        
        if now - last_time < cooldown:
            remaining = cooldown - time_since_last
            print(f"[AUTO-RESPONDER] ✗ Rule '{rule_id}' matched but in cooldown ({remaining:.0f}s remaining)")
            logger.info(f"Rule '{rule_id}' still in cooldown ({time_since_last:.1f}s / {cooldown}s), skipping")
            continue  # Still in cooldown
        
        # Update last response time
        last_response_times[rule_id] = now
        
        # Get response and substitute variables
        response = rule.get("response", "Auto-reply")
        response = substitute_variables(response, from_id, to_id, message_text, my_node, session_data)
        
        return response
    
    return None


# ── Helpers ─────────────────────────────────────────────────────────────────

def get_interface(session_data):
    """Return current interface or None."""
    return session_data["iface"]


def node_id_to_hex(num):
    """Convert numeric node ID to hex string like !02ed4dfc."""
    return f"!{num:08x}"


def hex_to_int(hex_id):
    """Convert hex node ID string to int."""
    return int(hex_id.replace("!", ""), 16)


def haversine(lat1, lon1, lat2, lon2):
    """Distance in km between two lat/lon points."""
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def get_node_info(node):
    """Extract useful fields from a node dict."""
    user = node.get("user", {})
    pos = node.get("position", {})
    metrics = node.get("deviceMetrics", {})
    return {
        "id": user.get("id", node_id_to_hex(node.get("num", 0))),
        "num": node.get("num", 0),
        "longName": user.get("longName", "Unknown"),
        "shortName": user.get("shortName", "????"),
        "hwModel": user.get("hwModel", "Unknown"),
        "role": user.get("role", "CLIENT"),
        "lat": pos.get("latitude"),
        "lon": pos.get("longitude"),
        "alt": pos.get("altitude"),
        "batteryLevel": metrics.get("batteryLevel"),
        "voltage": metrics.get("voltage"),
        "channelUtil": metrics.get("channelUtilization"),
        "airUtilTx": metrics.get("airUtilTx"),
        "uptime": metrics.get("uptimeSeconds"),
        "snr": node.get("snr"),
        "lastHeard": node.get("lastHeard"),
        "hopsAway": node.get("hopsAway"),
        "viaMqtt": node.get("viaMqtt", False),
        "isFavorite": node.get("isFavorite", False),
    }


def get_my_node(session_data):
    """Get our own node info."""
    i = session_data["iface"]
    if not i or not i.nodes:
        return None
    my_id = i.myInfo.my_node_num if hasattr(i, 'myInfo') and i.myInfo else None
    if my_id and my_id in i.nodesByNum:
        return get_node_info(i.nodesByNum[my_id])
    return None


# ── Packet listener ────────────────────────────────────────────────────────

def on_receive(packet, interface):
    """Called on every received packet - routes to correct session."""
    print(f"[PACKET] on_receive() called! packet keys: {list(packet.keys())}")
    try:
        # Log every packet that arrives
        from_id = packet.get('fromId', '?')
        portnum = packet.get('decoded', {}).get('portnum', 'UNKNOWN')
        has_rf = packet.get('rxSnr') is not None or packet.get('rxRssi') is not None
        source = "RF" if has_rf else "MQTT"
        print(f"[PACKET] {source} packet from {from_id}, port={portnum}")
        logger.info(f"on_receive: {source} packet from {from_id}, port={portnum}")
        
        # Get session_id from interface
        session_id = getattr(interface, '_session_id', None)
        if not session_id:
            logger.warning(f"Packet received but no session_id on interface (id={id(interface)}): {packet.get('fromId', '?')}")
            return  # No session associated with this interface
        
        # Get session data
        with sessions_lock:
            if session_id not in sessions:
                logger.warning(f"Session {session_id} not found in sessions dict")
                return
            session_data = sessions[session_id]
        
        logger.info(f"[Session {session_id[:8]}] Packet from {packet.get('fromId', '?')} - {packet.get('decoded', {}).get('portnum', 'UNKNOWN')}")
        
        # Apply message filter
        if should_filter_message(packet):
            return
        
        entry = {
            "time": datetime.now(timezone.utc).isoformat(),
            "from": packet.get("fromId", "?"),
            "to": packet.get("toId", "?"),
            "portnum": packet.get("decoded", {}).get("portnum", "UNKNOWN"),
            "snr": packet.get("rxSnr"),
            "rssi": packet.get("rxRssi"),
            "hopStart": packet.get("hopStart"),
            "hopLimit": packet.get("hopLimit"),
        }
        
        # Update MQTT health tracking
        now = time.time()
        from_id = packet.get("fromId", "?")
        my_node = get_my_node(session_data)
        my_id = my_node["id"] if my_node else None
        
        # Determine if this is an MQTT or RF message
        has_rf_metrics = packet.get("rxSnr") is not None or packet.get("rxRssi") is not None
        is_from_other_node = from_id != my_id
        
        if not has_rf_metrics and is_from_other_node:
            # No RF metrics and from another node = MQTT
            session_data["mqtt_health"]["last_mqtt_message"] = now
            session_data["mqtt_health"]["mqtt_message_count"] += 1
        elif has_rf_metrics:
            # Has RF metrics = RF message
            session_data["mqtt_health"]["last_rf_message"] = now
            session_data["mqtt_health"]["rf_message_count"] += 1
        
        # Add text if it's a text message
        decoded = packet.get("decoded", {})
        if decoded.get("portnum") == "TEXT_MESSAGE_APP":
            text = decoded.get("text", "")
            entry["text"] = text
            
            # Check for auto-responder trigger
            from_id = packet.get("fromId", "?")
            to_id = packet.get("toId", "?")
            print(f"[AUTO-RESPONDER] Checking message: '{text}' from={from_id} to={to_id}")
            logger.info(f"Checking auto-responder for text='{text}' from={from_id} to={to_id}")
            auto_response = check_autoresponder(text, from_id, to_id, session_data)
            
            if auto_response:
                print(f"[AUTO-RESPONDER] ✓ Sending response: '{auto_response}' to {from_id}")
                logger.info(f"Auto-responding to '{text}' from {from_id} with '{auto_response}'")
                try:
                    # Send response in a separate thread to avoid blocking
                    def send_auto_response():
                        time.sleep(2)
                        if session_data["iface"]:
                            dest = None if to_id == "^all" else hex_to_int(from_id)
                            session_data["iface"].sendText(auto_response, destinationId=dest, channelIndex=0)
                            
                            # Add auto-response to packet log
                            my_node = get_my_node(session_data)
                            my_id = my_node["id"] if my_node else "local"
                            dest_id = "^all" if to_id == "^all" else from_id
                            
                            with sessions_lock:
                                session_data["packet_log"].append({
                                    "time": datetime.now(timezone.utc).isoformat(),
                                    "from": my_id,
                                    "to": dest_id,
                                    "portnum": "TEXT_MESSAGE_APP",
                                    "text": auto_response,
                                    "snr": None,
                                    "rssi": None,
                                    "hopStart": None,
                                    "hopLimit": None,
                                    "outgoing": True
                                })
                                
                                if len(session_data["packet_log"]) > PACKET_LOG_MAX:
                                    session_data["packet_log"].pop(0)
                    
                    threading.Thread(target=send_auto_response, daemon=True).start()
                except Exception as e:
                    logger.error(f"Error sending auto-response: {e}")
        
        # Process traceroute responses
        if decoded.get("portnum") == "TRACEROUTE_APP":
            tr = decoded.get("traceroute", {})
            from_id = packet.get("fromId", "?")
            route = tr.get("route", [])
            route_back = tr.get("routeBack", [])
            snr_towards = tr.get("snrTowards", [])
            snr_back = tr.get("snrBack", [])
            
            logger.info(f"Traceroute response from {from_id}: route={route}, routeBack={route_back}")
            
            session_data["traceroute_results"][from_id] = {
                "route": route,
                "routeBack": route_back,
                "snrTowards": snr_towards,
                "snrBack": snr_back,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "status": "complete"
            }
            
            logger.info(f"Updated traceroute_results[{from_id}] to complete")

        session_data["packet_log"].append(entry)
        if len(session_data["packet_log"]) > PACKET_LOG_MAX:
            session_data["packet_log"].pop(0)
    except Exception as e:
        logger.warning(f"Error processing packet: {e}")


# Set up global packet listener for all sessions
pub.subscribe(on_receive, "meshtastic.receive")


# ── Routes ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/connect", methods=["POST"])
def connect():
    """Connect to a Meshtastic node via TCP."""
    session_data = get_session_data()
    session_id = get_session_id()
    data = request.json or {}
    host = data.get("host", "192.168.1.134")
    port = int(data.get("port", 4403))

    # Close existing connection for this session
    if session_data["iface"]:
        try:
            session_data["iface"].close()
        except:
            pass
        session_data["iface"] = None

    try:
        # Create interface
        session_data["iface"] = meshtastic.tcp_interface.TCPInterface(
            hostname=host, portNumber=port
        )
        
        # Store session_id with the interface so on_receive can find it
        session_data["iface"]._session_id = session_id
        logger.info(f"Connected session {session_id[:8]} to {host}:{port}, interface id={id(session_data['iface'])}")
        
        session_data["mqtt_health"]["connection_start"] = time.time()
        session_data["node_ip"] = host
        session_data["connected_at"] = time.time()
        time.sleep(2)  # Wait for node DB to populate

        my_node = get_my_node(session_data)
        node_count = len(session_data["iface"].nodes) if session_data["iface"].nodes else 0
        
        # Start beacon thread if beacon is enabled
        if beacon_config.get("enabled", False):
            print(f"[BEACON] Beacon is enabled, starting beacon thread for session {session_id[:8]}")
            logger.info(f"Beacon is enabled, starting beacon thread for session {session_id[:8]}")
            start_beacon(session_id)
        else:
            print(f"[BEACON] Beacon is disabled, not starting thread")

        return jsonify({
            "status": "connected",
            "myNode": my_node,
            "nodeCount": node_count
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/disconnect", methods=["POST"])
def disconnect():
    """Disconnect from the node."""
    session_data = get_session_data()
    if session_data["iface"]:
        try:
            session_data["iface"].close()
        except:
            pass
        session_data["iface"] = None
    return jsonify({"status": "disconnected"})


@app.route("/api/status")
def status():
    """Get connection status and basic info."""
    session_name = request.args.get('session_name')
    session_data = get_session_data(session_name)
    i = session_data["iface"]
    if not i:
        print(f"[STATUS] No interface for session '{session_name}'")
        return jsonify({"connected": False})
    try:
        # Use cached node info if available, otherwise fetch it
        my_node = session_data.get("my_node_info")
        if not my_node:
            print(f"[STATUS] No cached node info for session '{session_name}', fetching...")
            my_node = get_my_node(session_data)
            session_data["my_node_info"] = my_node  # Cache it
            print(f"[STATUS] Fetched and cached node: {my_node.get('id') if my_node else 'None'}")
        else:
            print(f"[STATUS] Using cached node info for session '{session_name}': {my_node.get('id') if my_node else 'None'}")
        
        result = {
            "connected": True,
            "myNode": my_node,
            "nodeCount": len(i.nodes) if i.nodes else 0
        }
        print(f"[STATUS] Returning myNode: id={my_node.get('id') if my_node else None}, longName={my_node.get('longName') if my_node else None}, shortName={my_node.get('shortName') if my_node else None}")
        return jsonify(result)
    except Exception as e:
        print(f"[STATUS] Error getting status for session '{session_name}': {e}")
        return jsonify({"connected": False})


@app.route("/api/nodes")
def nodes():
    """Get all nodes with optional filtering."""
    session_name = request.args.get('session_name')
    session_data = get_session_data(session_name)
    i = session_data["iface"]
    if not i or not i.nodes:
        return jsonify([])

    filter_type = request.args.get("filter", "all")  # all, rf, mqtt, local
    my_node = get_my_node(session_data)
    my_lat = my_node["lat"] if my_node else None
    my_lon = my_node["lon"] if my_node else None

    result = []
    for node_id, node in i.nodes.items():
        info = get_node_info(node)

        # Calculate distance if both positions known
        if my_lat and my_lon and info["lat"] and info["lon"]:
            info["distance_km"] = round(
                haversine(my_lat, my_lon, info["lat"], info["lon"]), 2
            )
        else:
            info["distance_km"] = None

        # Apply filter
        if filter_type == "rf" and info["viaMqtt"]:
            continue
        elif filter_type == "mqtt" and not info["viaMqtt"]:
            continue
        elif filter_type == "local" and (info["viaMqtt"] or (info["hopsAway"] is not None and info["hopsAway"] > 2)):
            continue

        result.append(info)

    # Sort: RF-direct first, then by hops, then by last heard
    result.sort(key=lambda n: (
        n["viaMqtt"],
        n["hopsAway"] if n["hopsAway"] is not None else 99,
        -(n["lastHeard"] or 0)
    ))

    return jsonify(result)


@app.route("/api/traceroute", methods=["POST"])
def traceroute():
    """Send a traceroute to a specific node."""
    session_data = get_session_data()
    i = session_data["iface"]
    if not i:
        return jsonify({"error": "Not connected"}), 400

    data = request.json or {}
    dest = data.get("dest")
    hop_limit = int(data.get("hopLimit", 6))

    if not dest:
        return jsonify({"error": "No destination specified"}), 400

    try:
        # Convert hex ID to int if needed
        if isinstance(dest, str) and dest.startswith("!"):
            dest_int = hex_to_int(dest)
        else:
            dest_int = int(dest)

        dest_hex = node_id_to_hex(dest_int)
        
        # Check if trying to traceroute self
        my_node = get_my_node(session_data)
        if my_node and (dest_hex == my_node["id"] or dest_int == my_node["num"]):
            return jsonify({"error": "Cannot traceroute to yourself"}), 400
        
        # Check if there's already a pending/recent traceroute to this node (within 30 seconds)
        if dest_hex in session_data["traceroute_results"]:
            existing = session_data["traceroute_results"][dest_hex]
            if existing.get("status") == "pending":
                return jsonify({"error": "Traceroute already in progress for this node"}), 400
            # Check timestamp to prevent spam
            try:
                last_time = datetime.fromisoformat(existing.get("timestamp", ""))
                if (datetime.now(timezone.utc) - last_time).total_seconds() < 30:
                    return jsonify({"error": "Please wait 30 seconds between traceroutes to the same node"}), 400
            except:
                pass
        
        session_data["traceroute_results"][dest_hex] = {
            "status": "pending",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "command": f"sendTraceRoute({dest_hex}, hopLimit={hop_limit})"
        }

        # Send traceroute in a thread so we don't block
        def do_traceroute():
            try:
                i.sendTraceRoute(dest_int, hopLimit=hop_limit, channelIndex=0)
                
                # Set a timeout to mark as failed if no response after 60 seconds
                def check_timeout():
                    time.sleep(60)
                    if dest_hex in session_data["traceroute_results"] and session_data["traceroute_results"][dest_hex].get("status") == "pending":
                        logger.warning(f"Traceroute to {dest_hex} timed out")
                        session_data["traceroute_results"][dest_hex] = {
                            "status": "timeout",
                            "error": "No response received within 60 seconds",
                            "timestamp": datetime.now(timezone.utc).isoformat()
                        }
                
                timeout_thread = threading.Thread(target=check_timeout, daemon=True)
                timeout_thread.start()
                
            except Exception as e:
                logger.error(f"Error sending traceroute to {dest_hex}: {e}")
                session_data["traceroute_results"][dest_hex] = {
                    "status": "error",
                    "error": str(e),
                    "timestamp": datetime.now(timezone.utc).isoformat()
                }

        t = threading.Thread(target=do_traceroute, daemon=True)
        t.start()

        return jsonify({"status": "sent", "dest": dest_hex})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/traceroute/results")
def traceroute_results_endpoint():
    """Get all traceroute results."""
    session_name = request.args.get('session_name')
    session_data = get_session_data(session_name)
    return jsonify(session_data["traceroute_results"])


@app.route("/api/traceroute/cancel", methods=["POST"])
def cancel_traceroute():
    """Cancel a pending traceroute."""
    session_data = get_session_data()
    data = request.json or {}
    node_id = data.get("nodeId")
    
    if not node_id:
        return jsonify({"error": "No nodeId specified"}), 400
    
    if node_id in session_data["traceroute_results"] and session_data["traceroute_results"][node_id].get("status") == "pending":
        session_data["traceroute_results"][node_id] = {
            "status": "cancelled",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "command": session_data["traceroute_results"][node_id].get("command", "")
        }
        return jsonify({"status": "cancelled", "nodeId": node_id})
    
    return jsonify({"error": "No pending traceroute found for this node"}), 404


@app.route("/api/sweep", methods=["POST"])
def sweep():
    """Automated RF sweep - traceroute all non-MQTT nodes to map RF reach."""
    session_data = get_session_data()
    i = session_data["iface"]
    if not i or not i.nodes:
        return jsonify({"error": "Not connected"}), 400

    if session_data["sweep_status"]["running"]:
        return jsonify({"error": "Sweep already running"}), 400

    # Get non-MQTT nodes (actual RF-reachable candidates)
    candidates = []
    for node_id, node in i.nodes.items():
        info = get_node_info(node)
        # Skip MQTT-only nodes and ourselves
        my_node = get_my_node(session_data)
        if my_node and info["id"] == my_node["id"]:
            continue
        if not info["viaMqtt"]:
            candidates.append(info)

    # Also include nodes with low hop count even if via MQTT
    # (they might be reachable over RF too)
    for node_id, node in i.nodes.items():
        info = get_node_info(node)
        if info["viaMqtt"] and info["hopsAway"] is not None and info["hopsAway"] <= 2:
            if not any(c["id"] == info["id"] for c in candidates):
                candidates.append(info)

    session_data["sweep_status"] = {
        "running": True,
        "current": None,
        "total": len(candidates),
        "completed": 0,
        "results": {}
    }

    def run_sweep():
        try:
            for idx, node_info in enumerate(candidates):
                # Check if cancelled before starting each node
                if not session_data["sweep_status"]["running"]:
                    logger.info(f"Sweep cancelled at {idx}/{len(candidates)}")
                    break

                dest_id = node_info["id"]
                session_data["sweep_status"]["current"] = dest_id
                session_data["sweep_status"]["completed"] = idx

                session_data["traceroute_results"][dest_id] = {
                    "status": "pending",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "command": f"sendTraceRoute({dest_id}, hopLimit=6)"
                }

                try:
                    dest_int = hex_to_int(dest_id)
                    i.sendTraceRoute(dest_int, hopLimit=6, channelIndex=0)
                    
                    # Wait for response
                    wait_time = 0
                    while wait_time < 30:
                        if not session_data["sweep_status"]["running"]:
                            session_data["traceroute_results"][dest_id] = {
                                "status": "cancelled",
                                "timestamp": datetime.now(timezone.utc).isoformat()
                            }
                            break
                        
                        time.sleep(1)
                        wait_time += 1
                        
                        if dest_id in session_data["traceroute_results"] and session_data["traceroute_results"][dest_id].get("status") == "complete":
                            break

                    # Only mark as timeout if not cancelled and not complete
                    if session_data["sweep_status"]["running"] and session_data["traceroute_results"].get(dest_id, {}).get("status") != "complete":
                        session_data["traceroute_results"][dest_id] = {
                            "status": "timeout",
                            "timestamp": datetime.now(timezone.utc).isoformat()
                        }

                except Exception as e:
                    session_data["traceroute_results"][dest_id] = {
                        "status": "error",
                        "error": str(e),
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    }

                session_data["sweep_status"]["results"][dest_id] = session_data["traceroute_results"].get(dest_id, {})
                
                if not session_data["sweep_status"]["running"]:
                    break
                    
                time.sleep(3)

            session_data["sweep_status"]["completed"] = len(candidates)
            
        finally:
            session_data["sweep_status"]["running"] = False
            session_data["sweep_status"]["current"] = None
            logger.info(f"Sweep finished: {session_data['sweep_status']['completed']}/{session_data['sweep_status']['total']} nodes tested")

    t = threading.Thread(target=run_sweep, daemon=True)
    t.start()

    return jsonify({
        "status": "started",
        "total": len(candidates),
        "candidates": [c["id"] for c in candidates]
    })


@app.route("/api/sweep/status")
def sweep_status_endpoint():
    """Get current sweep status."""
    session_name = request.args.get('session_name')
    session_data = get_session_data(session_name)
    return jsonify(session_data["sweep_status"])


@app.route("/api/sweep/stop", methods=["POST"])
def sweep_stop():
    """Stop a running sweep."""
    session_data = get_session_data()
    session_data["sweep_status"]["running"] = False
    return jsonify({"status": "stopped"})


@app.route("/api/send", methods=["POST"])
def send_message():
    """Send a text message to the mesh or a specific node."""
    data = request.json or {}
    session_name = data.get('session_name')
    session_data = get_session_data(session_name)
    i = session_data["iface"]
    if not i:
        return jsonify({"error": "Not connected"}), 400

    data = request.json or {}
    text = data.get("text", "")
    dest = data.get("dest")  # None = broadcast
    channel = int(data.get("channel", 0))

    if not text:
        return jsonify({"error": "No text specified"}), 400

    try:
        if dest:
            if isinstance(dest, str) and dest.startswith("!"):
                dest = hex_to_int(dest)
            i.sendText(text, destinationId=dest, channelIndex=channel)
            dest_id = node_id_to_hex(dest)
        else:
            i.sendText(text, channelIndex=channel)
            dest_id = "^all"
            # Track MQTT sent count for broadcasts (if MQTT uplink enabled)
            session_data["mqtt_health"]["mqtt_sent_count"] += 1
        
        # Add outgoing message to packet log
        my_node = get_my_node(session_data)
        my_id = my_node["id"] if my_node else "local"
        
        session_data["packet_log"].append({
            "time": datetime.now(timezone.utc).isoformat(),
            "from": my_id,
            "to": dest_id,
            "portnum": "TEXT_MESSAGE_APP",
            "text": text,
            "snr": None,
            "rssi": None,
            "hopStart": None,
            "hopLimit": None,
            "outgoing": True
        })
        
        if len(session_data["packet_log"]) > PACKET_LOG_MAX:
            session_data["packet_log"].pop(0)

        return jsonify({"status": "sent"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/packets")
def packets():
    """Get recent packet log."""
    session_name = request.args.get('session_name')
    session_data = get_session_data(session_name)
    since = request.args.get("since")
    if since:
        filtered = [p for p in session_data["packet_log"] if p["time"] > since]
        return jsonify(filtered)
    return jsonify(session_data["packet_log"][-100:])  # Last 100 by default


@app.route("/api/device/config")
def device_config():
    """Get device configuration (localConfig, moduleConfig, channels)."""
    session_name = request.args.get('session_name')
    session_data = get_session_data(session_name)
    i = session_data["iface"]
    if not i:
        return jsonify({"error": "Not connected"}), 400
    
    try:
        config = {}
        
        # Get local config
        if hasattr(i, 'localNode') and i.localNode:
            if hasattr(i.localNode, 'localConfig'):
                config['localConfig'] = {
                    'device': MessageToDict(i.localNode.localConfig.device) if hasattr(i.localNode.localConfig, 'device') else {},
                    'position': MessageToDict(i.localNode.localConfig.position) if hasattr(i.localNode.localConfig, 'position') else {},
                    'power': MessageToDict(i.localNode.localConfig.power) if hasattr(i.localNode.localConfig, 'power') else {},
                    'network': MessageToDict(i.localNode.localConfig.network) if hasattr(i.localNode.localConfig, 'network') else {},
                    'display': MessageToDict(i.localNode.localConfig.display) if hasattr(i.localNode.localConfig, 'display') else {},
                    'lora': MessageToDict(i.localNode.localConfig.lora) if hasattr(i.localNode.localConfig, 'lora') else {},
                    'bluetooth': MessageToDict(i.localNode.localConfig.bluetooth) if hasattr(i.localNode.localConfig, 'bluetooth') else {},
                }
            
            if hasattr(i.localNode, 'moduleConfig'):
                config['moduleConfig'] = {
                    'mqtt': MessageToDict(i.localNode.moduleConfig.mqtt) if hasattr(i.localNode.moduleConfig, 'mqtt') else {},
                    'serial': MessageToDict(i.localNode.moduleConfig.serial) if hasattr(i.localNode.moduleConfig, 'serial') else {},
                    'externalNotification': MessageToDict(i.localNode.moduleConfig.external_notification) if hasattr(i.localNode.moduleConfig, 'external_notification') else {},
                    'storeForward': MessageToDict(i.localNode.moduleConfig.store_forward) if hasattr(i.localNode.moduleConfig, 'store_forward') else {},
                    'rangeTest': MessageToDict(i.localNode.moduleConfig.range_test) if hasattr(i.localNode.moduleConfig, 'range_test') else {},
                    'telemetry': MessageToDict(i.localNode.moduleConfig.telemetry) if hasattr(i.localNode.moduleConfig, 'telemetry') else {},
                    'cannedMessage': MessageToDict(i.localNode.moduleConfig.canned_message) if hasattr(i.localNode.moduleConfig, 'canned_message') else {},
                    'audio': MessageToDict(i.localNode.moduleConfig.audio) if hasattr(i.localNode.moduleConfig, 'audio') else {},
                    'remoteHardware': MessageToDict(i.localNode.moduleConfig.remote_hardware) if hasattr(i.localNode.moduleConfig, 'remote_hardware') else {},
                    'neighborInfo': MessageToDict(i.localNode.moduleConfig.neighbor_info) if hasattr(i.localNode.moduleConfig, 'neighbor_info') else {},
                    'ambientLighting': MessageToDict(i.localNode.moduleConfig.ambient_lighting) if hasattr(i.localNode.moduleConfig, 'ambient_lighting') else {},
                    'detectionSensor': MessageToDict(i.localNode.moduleConfig.detection_sensor) if hasattr(i.localNode.moduleConfig, 'detection_sensor') else {},
                }
            
            if hasattr(i.localNode, 'channels'):
                config['channels'] = []
                for idx, channel in enumerate(i.localNode.channels):
                    if channel:
                        config['channels'].append({
                            'index': idx,
                            'settings': MessageToDict(channel.settings) if hasattr(channel, 'settings') else {},
                            'role': str(channel.role) if hasattr(channel, 'role') else 'DISABLED'
                        })
        
        return jsonify(config)
    except Exception as e:
        logger.error(f"Error fetching device config: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/node/<node_id>")
def node_detail(node_id):
    """Get detailed info for a specific node."""
    session_name = request.args.get('session_name')
    session_data = get_session_data(session_name)
    i = session_data["iface"]
    if not i or not i.nodes:
        return jsonify({"error": "Not connected"}), 400

    if node_id in i.nodes:
        node = i.nodes[node_id]
        info = get_node_info(node)

        # Add traceroute result if available
        info["traceroute"] = session_data["traceroute_results"].get(node_id)

        # Calculate distance
        my_node = get_my_node(session_data)
        if my_node and my_node["lat"] and my_node["lon"] and info["lat"] and info["lon"]:
            info["distance_km"] = round(
                haversine(my_node["lat"], my_node["lon"], info["lat"], info["lon"]), 2
            )

        return jsonify(info)

    return jsonify({"error": "Node not found"}), 404


@app.route("/api/mqtt/health", methods=["GET"])
def get_mqtt_health():
    """Get MQTT health status and metrics."""
    session_name = request.args.get('session_name')
    session_data = get_session_data(session_name)
    
    now = time.time()
    
    # Calculate time since last messages
    last_mqtt_ago = (now - session_data["mqtt_health"]["last_mqtt_message"]) if session_data["mqtt_health"]["last_mqtt_message"] else None
    last_rf_ago = (now - session_data["mqtt_health"]["last_rf_message"]) if session_data["mqtt_health"]["last_rf_message"] else None
    uptime = (now - session_data["mqtt_health"]["connection_start"]) if session_data["mqtt_health"]["connection_start"] else 0
    
    # Determine health status
    health_status = "unknown"
    health_color = "gray"
    health_message = "No data yet"
    
    if session_data["mqtt_health"]["last_mqtt_message"]:
        if last_mqtt_ago < 300:  # < 5 minutes
            health_status = "healthy"
            health_color = "green"
            health_message = "MQTT active"
        elif last_mqtt_ago < 900:  # < 15 minutes
            health_status = "slow"
            health_color = "yellow"
            health_message = "MQTT quiet"
        else:
            health_status = "stalled"
            health_color = "red"
            health_message = "No MQTT traffic"
    elif uptime > 600:  # Connected for 10+ min but no MQTT
        health_status = "stalled"
        health_color = "red"
        health_message = "No MQTT received"
    
    # Get mqtt.root from device config
    mqtt_root = "unknown"
    i = session_data["iface"]
    if i:
        try:
            mqtt_config = i.localNode.moduleConfig.mqtt if hasattr(i.localNode, 'moduleConfig') else None
            if mqtt_config:
                from google.protobuf.json_format import MessageToDict
                config_dict = MessageToDict(mqtt_config)
                mqtt_root = config_dict.get("root", "msh/US")
        except:
            pass
    
    return jsonify({
        "last_mqtt_ago": last_mqtt_ago,
        "last_rf_ago": last_rf_ago,
        "mqtt_count": session_data["mqtt_health"]["mqtt_message_count"],
        "rf_count": session_data["mqtt_health"]["rf_message_count"],
        "mqtt_sent": session_data["mqtt_health"]["mqtt_sent_count"],
        "uptime": uptime,
        "health_status": health_status,
        "health_color": health_color,
        "health_message": health_message,
        "mqtt_root": mqtt_root
    })


@app.route("/api/mqtt/bridge/status", methods=["GET"])
def get_mqtt_bridge_status():
    """Check if local MQTT bridge is running and get configuration."""
    import subprocess
    import os
    
    bridge_config_path = os.path.join(os.path.dirname(__file__), "mqtt-bridge", "config.json")
    
    # Check if mosquitto service is running
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "mosquitto"],
            capture_output=True,
            text=True,
            timeout=2
        )
        is_running = result.returncode == 0
    except:
        is_running = False
    
    # Load bridge configuration if available
    bridge_config = None
    if os.path.exists(bridge_config_path):
        try:
            with open(bridge_config_path, 'r') as f:
                bridge_config = json.load(f)
        except:
            pass
    
    return jsonify({
        "installed": os.path.exists(bridge_config_path),
        "running": is_running,
        "config": bridge_config
    })


@app.route("/api/store-forward/status")
def get_store_forward_status():
    """Get Store & Forward module status and statistics."""
    session_name = request.args.get('session_name')
    session_data = get_session_data(session_name)
    i = session_data["iface"]
    if not i:
        return jsonify({"error": "Not connected"}), 400
    
    try:
        sf_config = None
        print(f"[STORE&FORWARD] Checking Store & Forward status for session '{session_name}'")
        print(f"[STORE&FORWARD] Has localNode: {hasattr(i, 'localNode')}")
        
        if hasattr(i, 'localNode') and i.localNode:
            print(f"[STORE&FORWARD] Has moduleConfig: {hasattr(i.localNode, 'moduleConfig')}")
            if hasattr(i.localNode, 'moduleConfig'):
                # Check for store_forward using protobuf HasField method
                has_sf = i.localNode.moduleConfig.HasField('store_forward')
                print(f"[STORE&FORWARD] Has store_forward field: {has_sf}")
                
                if has_sf:
                    sf_raw = i.localNode.moduleConfig.store_forward
                    print(f"[STORE&FORWARD] Raw protobuf fields: {dir(sf_raw)}")
                    sf_config = MessageToDict(sf_raw)
                    print(f"[STORE&FORWARD] Full config dict: {sf_config}")
                else:
                    print(f"[STORE&FORWARD] store_forward field not set in moduleConfig")
            else:
                print(f"[STORE&FORWARD] moduleConfig not found on localNode")
        else:
            print(f"[STORE&FORWARD] localNode not available")
        
        if not sf_config:
            result = {
                "enabled": False,
                "available": False,
                "message": "Store & Forward module not available on this device"
            }
            print(f"[STORE&FORWARD] Returning: {result}")
            return jsonify(result)
        
        # Get statistics from the module
        stats = {
            "enabled": sf_config.get("enabled", False),
            "available": True,
            "heartbeat": sf_config.get("heartbeat", False),
            "records": sf_config.get("records", 0),
            "historyReturnMax": sf_config.get("historyReturnMax", 0),
            "historyReturnWindow": sf_config.get("historyReturnWindow", 0),
            "isServer": sf_config.get("isServer", False)
        }
        
        print(f"[STORE&FORWARD] Returning stats: enabled={stats['enabled']}, available={stats['available']}, isServer={stats['isServer']}, records={stats['records']}")
        return jsonify(stats)
    except Exception as e:
        logger.error(f"Error getting Store & Forward status: {e}")
        return jsonify({
            "enabled": False,
            "available": False,
            "error": str(e)
        }), 500


@app.route("/api/device/mqtt/config", methods=["GET"])
def get_device_mqtt_config():
    """Get current MQTT configuration from the connected device."""
    session_name = request.args.get('session_name')
    session_data = get_session_data(session_name)
    i = session_data["iface"]
    if not i:
        return jsonify({"error": "Not connected"}), 400
    
    try:
        # Get MQTT module config
        mqtt_config = i.localNode.moduleConfig.mqtt if hasattr(i.localNode, 'moduleConfig') else None
        
        if mqtt_config:
            config_dict = MessageToDict(mqtt_config)
            return jsonify({
                "enabled": config_dict.get("enabled", False),
                "address": config_dict.get("address", ""),
                "username": config_dict.get("username", ""),
                "password": config_dict.get("password", ""),
                "encryptionEnabled": config_dict.get("encryptionEnabled", False),
                "tlsEnabled": config_dict.get("tlsEnabled", False),
                "root": config_dict.get("root", "msh/US"),
                "proxyToClientEnabled": config_dict.get("proxyToClientEnabled", False),
                "mapReportingEnabled": config_dict.get("mapReportingEnabled", False),
                "jsonEnabled": config_dict.get("jsonEnabled", False)
            })
        else:
            return jsonify({"error": "Could not read MQTT config"}), 500
    except Exception as e:
        logger.error(f"Error reading MQTT config: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/device/mqtt/config", methods=["POST"])
def update_device_mqtt_config():
    """Update MQTT configuration on the connected device."""
    session_data = get_session_data()
    i = session_data["iface"]
    if not i:
        return jsonify({"error": "Not connected"}), 400
    
    data = request.json or {}
    
    try:
        # Update MQTT configuration
        if "enabled" in data:
            i.localNode.moduleConfig.mqtt.enabled = data["enabled"]
        if "address" in data:
            i.localNode.moduleConfig.mqtt.address = data["address"]
        if "username" in data:
            i.localNode.moduleConfig.mqtt.username = data["username"]
        if "password" in data:
            i.localNode.moduleConfig.mqtt.password = data["password"]
        if "encryptionEnabled" in data:
            i.localNode.moduleConfig.mqtt.encryption_enabled = data["encryptionEnabled"]
        if "tlsEnabled" in data:
            i.localNode.moduleConfig.mqtt.tls_enabled = data["tlsEnabled"]
        if "root" in data:
            i.localNode.moduleConfig.mqtt.root = data["root"]
        
        # Write configuration to device
        i.localNode.writeConfig("mqtt")
        
        logger.info(f"Updated MQTT config: {data}")
        
        return jsonify({
            "status": "success",
            "message": "MQTT configuration updated. Device will reboot to apply changes."
        })
    except Exception as e:
        logger.error(f"Error updating MQTT config: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/message-filter", methods=["GET"])
def get_message_filter():
    """Get message filter configuration."""
    return jsonify(message_filter)


@app.route("/api/message-filter", methods=["POST"])
def update_message_filter():
    """Update message filter configuration."""
    global message_filter
    data = request.json or {}
    
    message_filter["enabled"] = data.get("enabled", False)
    message_filter["filter_mode"] = data.get("filter_mode", "allowlist")
    message_filter["node_ids"] = data.get("node_ids", [])
    message_filter["portnums"] = data.get("portnums", [])
    
    save_message_filter()
    
    return jsonify({
        "status": "success",
        "message": "Message filter updated",
        "filter": message_filter
    })


@app.route("/api/device/reboot", methods=["POST"])
def reboot_device():
    """Reboot the connected device."""
    session_data = get_session_data()
    i = session_data["iface"]
    if not i:
        return jsonify({"error": "Not connected"}), 400
    
    try:
        i.localNode.reboot()
        logger.info("Device reboot initiated")
        return jsonify({
            "status": "success",
            "message": "Device is rebooting..."
        })
    except Exception as e:
        logger.error(f"Error rebooting device: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/device/shutdown", methods=["POST"])
def shutdown_device():
    """Shutdown the connected device."""
    session_data = get_session_data()
    i = session_data["iface"]
    if not i:
        return jsonify({"error": "Not connected"}), 400
    
    try:
        i.localNode.shutdown()
        logger.info("Device shutdown initiated")
        return jsonify({
            "status": "success",
            "message": "Device is shutting down..."
        })
    except Exception as e:
        logger.error(f"Error shutting down device: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/device/factory-reset", methods=["POST"])
def factory_reset_device():
    """Factory reset the connected device."""
    session_data = get_session_data()
    i = session_data["iface"]
    if not i:
        return jsonify({"error": "Not connected"}), 400
    
    try:
        i.localNode.factoryReset()
        logger.info("Device factory reset initiated")
        return jsonify({
            "status": "success",
            "message": "Device is performing factory reset..."
        })
    except Exception as e:
        logger.error(f"Error factory resetting device: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/autoresponder", methods=["GET"])
def get_autoresponder_config():
    """Get auto-responder configuration."""
    return jsonify(autoresponder_config)


@app.route("/api/autoresponder", methods=["POST"])
def update_autoresponder_config():
    """Update auto-responder configuration."""
    global autoresponder_config
    data = request.json or {}
    autoresponder_config = data
    save_autoresponder_config()
    return jsonify({"status": "saved"})


@app.route("/api/beacon", methods=["GET"])
def get_beacon_config():
    """Get beacon configuration."""
    return jsonify(beacon_config)


@app.route("/api/beacon", methods=["POST"])
def update_beacon_config():
    """Update beacon configuration."""
    global beacon_config
    data = request.json or {}
    
    # Preserve available_variables and last_beacon_time if not in incoming data
    old_enabled = beacon_config.get("enabled", False)
    
    # If available_variables not in incoming data, preserve from existing config
    if "available_variables" not in data:
        data["available_variables"] = beacon_config.get("available_variables", {})
    
    # If last_beacon_time not in incoming data, preserve from existing config
    if "last_beacon_time" not in data and "last_beacon_time" in beacon_config:
        data["last_beacon_time"] = beacon_config.get("last_beacon_time")
    
    beacon_config = data
    save_beacon_config()
    
    # Handle beacon state changes
    session_id = get_session_id()
    new_enabled = beacon_config.get("enabled", False)
    
    if new_enabled and not old_enabled:
        # Beacon was just enabled - start it
        start_beacon(session_id)
    elif not new_enabled and old_enabled:
        # Beacon was just disabled - stop it
        stop_beacon(session_id)
    elif new_enabled:
        # Beacon is enabled and config changed - restart it
        start_beacon(session_id)
    
    return jsonify({"status": "saved"})


@app.route("/api/beacon/start", methods=["POST"])
def start_beacon_endpoint():
    """Manually start beacon."""
    session_id = get_session_id()
    session_data = get_session_data()
    
    if not session_data["iface"]:
        return jsonify({"error": "Not connected"}), 400
    
    beacon_config["enabled"] = True
    save_beacon_config()
    start_beacon(session_id)
    
    return jsonify({"status": "started"})


@app.route("/api/beacon/stop", methods=["POST"])
def stop_beacon_endpoint():
    """Manually stop beacon."""
    session_id = get_session_id()
    
    beacon_config["enabled"] = False
    save_beacon_config()
    stop_beacon(session_id)
    
    return jsonify({"status": "stopped"})


@app.route("/api/beacon/test", methods=["POST"])
def test_beacon():
    """Send a test beacon message immediately."""
    session_data = get_session_data()
    
    if not session_data["iface"]:
        return jsonify({"error": "Not connected"}), 400
    
    template = beacon_config.get("message_template", "")
    if not template:
        return jsonify({"error": "No beacon message template configured"}), 400
    
    try:
        message = substitute_beacon_variables(template, session_data)
        channel = beacon_config.get("channel", 0)
        session_data["iface"].sendText(message, channelIndex=channel)
        
        # Add to packet log
        my_node = get_my_node(session_data)
        my_id = my_node["id"] if my_node else "local"
        
        session_data["packet_log"].append({
            "time": datetime.now(timezone.utc).isoformat(),
            "from": my_id,
            "to": "^all",
            "portnum": "TEXT_MESSAGE_APP",
            "text": message,
            "snr": None,
            "rssi": None,
            "hopStart": None,
            "hopLimit": None,
            "outgoing": True
        })
        
        if len(session_data["packet_log"]) > PACKET_LOG_MAX:
            session_data["packet_log"].pop(0)
        
        return jsonify({"status": "sent", "message": message})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Persistent Session API Endpoints ───────────────────────────────────────

@app.route("/api/sessions")
def list_sessions():
    """Get list of all persistent sessions."""
    with persistent_sessions_lock:
        session_list = []
        for name, config in persistent_sessions.items():
            session_id = config["session_id"]
            
            # Check if session is currently connected
            with sessions_lock:
                is_connected = session_id in sessions and sessions[session_id]["iface"] is not None
            
            session_list.append({
                "name": name,
                "session_id": session_id,
                "host": config["host"],
                "port": config["port"],
                "auto_connect": config.get("auto_connect", False),
                "connected": is_connected,
                "created_at": config.get("created_at"),
                "last_connected": config.get("last_connected"),
                "beacon_enabled": config.get("beacon_enabled", False),
                "autoresponder_enabled": config.get("autoresponder_enabled", False)
            })
        
        return jsonify({"sessions": session_list})


@app.route("/api/sessions/create", methods=["POST"])
def create_session():
    """Create a new persistent session."""
    data = request.json or {}
    name = data.get("name", "").strip()
    host = data.get("host", "").strip()
    port = int(data.get("port", 4403))
    auto_connect = data.get("auto_connect", False)
    
    if not name:
        return jsonify({"error": "Session name is required"}), 400
    if not host:
        return jsonify({"error": "Host is required"}), 400
    
    result = create_persistent_session(name, host, port, auto_connect)
    
    if "error" in result:
        return jsonify(result), 400
    
    return jsonify(result)


@app.route("/api/sessions/<session_name>/delete", methods=["POST"])
def delete_session(session_name):
    """Delete a persistent session."""
    result = delete_persistent_session(session_name)
    
    if "error" in result:
        return jsonify(result), 404
    
    return jsonify(result)


@app.route("/api/sessions/<session_name>/connect", methods=["POST"])
def connect_session(session_name):
    """Connect to a persistent session."""
    session_id, session_data = get_session_by_name(session_name)
    
    if not session_id:
        return jsonify({"error": "Session not found"}), 404
    
    # Get session config
    with persistent_sessions_lock:
        session_config = persistent_sessions.get(session_name)
        if not session_config:
            return jsonify({"error": "Session config not found"}), 404
        
        host = session_config["host"]
        port = session_config["port"]
    
    # Ensure session data exists
    with sessions_lock:
        if session_id not in sessions:
            sessions[session_id] = create_session_data()
        session_data = sessions[session_id]
    
    # Close existing connection if any
    if session_data["iface"]:
        try:
            session_data["iface"].close()
        except:
            pass
        session_data["iface"] = None
    
    try:
        # Create interface
        session_data["iface"] = meshtastic.tcp_interface.TCPInterface(
            hostname=host, portNumber=port
        )
        
        # Store session_id with the interface
        session_data["iface"]._session_id = session_id
        logger.info(f"Connected persistent session '{session_name}' ({session_id[:8]}) to {host}:{port}")
        
        session_data["mqtt_health"]["connection_start"] = time.time()
        session_data["node_ip"] = host
        session_data["connected_at"] = time.time()
        time.sleep(2)  # Wait for node DB to populate
        
        # Update last connected time
        update_persistent_session_config(session_name, {
            "last_connected": datetime.now(timezone.utc).isoformat()
        })
        
        # Start beacon if enabled globally
        if beacon_config.get("enabled", False):
            print(f"[BEACON] Beacon is enabled, starting thread for session '{session_name}'")
            start_beacon(session_id)
        
        my_node = get_my_node(session_data)
        node_count = len(session_data["iface"].nodes) if session_data["iface"].nodes else 0
        
        return jsonify({
            "status": "connected",
            "session_name": session_name,
            "session_id": session_id,
            "myNode": my_node,
            "nodeCount": node_count
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/sessions/<session_name>/disconnect", methods=["POST"])
def disconnect_session(session_name):
    """Disconnect a persistent session."""
    session_id, session_data = get_session_by_name(session_name)
    
    if not session_id or not session_data:
        return jsonify({"error": "Session not found"}), 404
    
    if session_data["iface"]:
        try:
            session_data["iface"].close()
        except:
            pass
        session_data["iface"] = None
    
    # Stop beacon
    stop_beacon(session_id)
    
    return jsonify({"status": "disconnected"})


@app.route("/api/sessions/<session_name>/update", methods=["POST"])
def update_session(session_name):
    """Update persistent session configuration."""
    data = request.json or {}
    
    # Only allow updating certain fields
    allowed_updates = {}
    if "auto_connect" in data:
        allowed_updates["auto_connect"] = data["auto_connect"]
    if "beacon_enabled" in data:
        allowed_updates["beacon_enabled"] = data["beacon_enabled"]
    if "beacon_interval" in data:
        allowed_updates["beacon_interval"] = data["beacon_interval"]
    if "beacon_message" in data:
        allowed_updates["beacon_message"] = data["beacon_message"]
    if "beacon_channel" in data:
        allowed_updates["beacon_channel"] = data["beacon_channel"]
    if "autoresponder_enabled" in data:
        allowed_updates["autoresponder_enabled"] = data["autoresponder_enabled"]
    if "autoresponder_rules" in data:
        allowed_updates["autoresponder_rules"] = data["autoresponder_rules"]
    
    result = update_persistent_session_config(session_name, allowed_updates)
    
    if "error" in result:
        return jsonify(result), 404
    
    return jsonify(result)


# ── Main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Load configs on startup
    load_autoresponder_config()
    load_beacon_config()
    load_persistent_sessions()
    
    print("=" * 60)
    print("  Meshtastic Dashboard")
    print("  Open http://localhost:5000 in your browser")
    print(f"  Auto-responder: {'ENABLED' if autoresponder_config.get('enabled') else 'DISABLED'} ({len(autoresponder_config.get('rules', []))} rules)")
    print(f"  Beacon: {'ENABLED' if beacon_config.get('enabled') else 'DISABLED'} (interval: {beacon_config.get('interval_minutes', 60)}min)")
    print(f"  Persistent Sessions: {len(persistent_sessions)} configured")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=True, threaded=True)
