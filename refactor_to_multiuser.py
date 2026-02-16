#!/usr/bin/env python3
"""
Script to refactor app.py into app_multiuser.py with session-based architecture.
This systematically replaces global state with session-based state.
"""

import re

def refactor_app():
    """Refactor app.py to app_multiuser.py with session support."""
    
    with open('app.py', 'r') as f:
        content = f.read()
    
    # 1. Update imports
    content = content.replace(
        'from flask import Flask, render_template, jsonify, request',
        'from flask import Flask, render_template, jsonify, request, session'
    )
    
    content = content.replace(
        'import os\nfrom datetime import',
        'import os\nimport secrets\nfrom datetime import'
    )
    
    # 2. Add Flask session config after app creation
    config_addition = """app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = 3600  # 1 hour
"""
    content = content.replace(
        "app.config['TEMPLATES_AUTO_RELOAD'] = True",
        "app.config['TEMPLATES_AUTO_RELOAD'] = True\n" + config_addition
    )
    
    # 3. Replace global state section with session-based state
    old_global_state = """# ── Global State ────────────────────────────────────────────────────────────
iface = None
iface_lock = threading.Lock()
packet_log = []          # recent packets (max 500)
traceroute_results = {}  # nodeId -> { route, snr, timestamp, status }
sweep_status = {
    "running": False,
    "current": None,
    "total": 0,
    "completed": 0,
    "results": {}
}
PACKET_LOG_MAX = 500

# MQTT health tracking
mqtt_health = {
    "last_mqtt_message": None,      # timestamp of last MQTT message received
    "last_rf_message": None,        # timestamp of last RF message received
    "mqtt_message_count": 0,        # total MQTT messages received
    "rf_message_count": 0,          # total RF messages received
    "mqtt_sent_count": 0,           # total messages sent via MQTT
    "connection_start": None        # when we connected
}"""
    
    new_session_state = """# ── Session-Based State ────────────────────────────────────────────────────
# Each session gets its own connection and state
sessions = {}  # session_id -> session_data
sessions_lock = threading.Lock()
PACKET_LOG_MAX = 500
SESSION_TIMEOUT = 3600  # 1 hour idle timeout
MAX_SESSIONS = 10  # Limit concurrent sessions

def create_session_data():
    \"\"\"Create a new session data structure.\"\"\"
    return {
        "iface": None,
        "packet_log": [],
        "traceroute_results": {},
        "traceroute_pending": {},
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
        "node_ip": None
    }

def get_session_id():
    \"\"\"Get or create session ID for current request.\"\"\"
    if 'session_id' not in session:
        session['session_id'] = secrets.token_hex(16)
        session.permanent = True
    return session['session_id']

def get_session_data():
    \"\"\"Get session data for current user, creating if needed.\"\"\"
    session_id = get_session_id()
    
    with sessions_lock:
        if session_id not in sessions:
            if len(sessions) >= MAX_SESSIONS:
                cleanup_old_sessions(force_one=True)
            sessions[session_id] = create_session_data()
            logger.info(f"Created new session: {session_id}")
        
        sessions[session_id]["last_activity"] = time.time()
        return sessions[session_id]

def cleanup_old_sessions(force_one=False):
    \"\"\"Remove sessions that have been idle too long.\"\"\"
    now = time.time()
    to_remove = []
    
    for sid, sdata in sessions.items():
        age = now - sdata["last_activity"]
        if age > SESSION_TIMEOUT or (force_one and not to_remove):
            to_remove.append(sid)
            if sdata["iface"]:
                try:
                    sdata["iface"].close()
                except:
                    pass
    
    for sid in to_remove:
        del sessions[sid]
        logger.info(f"Cleaned up session: {sid}")
    
    return len(to_remove)

def cleanup_thread():
    \"\"\"Background thread to clean up old sessions.\"\"\"
    while True:
        time.sleep(300)
        with sessions_lock:
            cleanup_old_sessions()

cleanup_worker = threading.Thread(target=cleanup_thread, daemon=True)
cleanup_worker.start()"""
    
    content = content.replace(old_global_state, new_session_state)
    
    # 4. Update helper functions to accept session_data
    content = content.replace(
        'def get_interface():\n    """Return current interface or None."""\n    global iface\n    return iface',
        'def get_interface(session_data):\n    """Return current interface or None."""\n    return session_data["iface"]'
    )
    
    content = content.replace(
        'def get_my_node():\n    """Get info about the connected node."""\n    i = get_interface()',
        'def get_my_node(session_data):\n    """Get info about the connected node."""\n    i = session_data["iface"]'
    )
    
    # 5. Update check_autoresponder to accept session_data
    content = re.sub(
        r'def check_autoresponder\(message_text, from_id, to_id\):',
        'def check_autoresponder(message_text, from_id, to_id, session_data):',
        content
    )
    
    content = content.replace(
        'my_node = get_my_node()\n    if my_node',
        'my_node = get_my_node(session_data)\n    if my_node'
    )
    
    content = re.sub(
        r'response = substitute_variables\(response, from_id, to_id, message_text, my_node\)',
        'response = substitute_variables(response, from_id, to_id, message_text, my_node, session_data)',
        content
    )
    
    content = re.sub(
        r'def substitute_variables\(response_text, from_id, to_id, message_text, my_node\):',
        'def substitute_variables(response_text, from_id, to_id, message_text, my_node, session_data):',
        content
    )
    
    content = content.replace(
        '"""Replace variables in response text with actual values."""\n    i = get_interface()',
        '"""Replace variables in response text with actual values."""\n    i = session_data["iface"]'
    )
    
    # Save the refactored version
    with open('app_multiuser.py', 'w') as f:
        f.write(content)
    
    print("✓ Created app_multiuser.py with session management framework")
    print("⚠ Note: Routes still need manual updating to use get_session_data()")
    print("  This provides the foundation - routes need to call get_session_data()")
    print("  and use session_data instead of globals")

if __name__ == '__main__':
    refactor_app()
