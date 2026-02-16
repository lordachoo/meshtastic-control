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
from datetime import datetime, timezone
from flask import Flask, render_template, jsonify, request
from google.protobuf.json_format import MessageToDict

import meshtastic
import meshtastic.tcp_interface
from pubsub import pub

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True

# Auto-responder configuration
AUTORESPONDER_FILE = "autoresponder.json"
autoresponder_config = {"enabled": True, "rules": []}
last_response_times = {}  # Track cooldowns per rule

# ── Global State ────────────────────────────────────────────────────────────
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
}

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
            return True  # Filter out (not in allowlist)
        elif filter_mode == "blocklist" and node_match:
            return True  # Filter out (in blocklist)
    
    # Check portnum filtering
    if portnums:
        portnum_match = portnum in portnums
        if filter_mode == "allowlist" and not portnum_match:
            return True  # Filter out (not in allowlist)
        elif filter_mode == "blocklist" and portnum_match:
            return True  # Filter out (in blocklist)
    
    return False  # Don't filter

load_message_filter()


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


def substitute_variables(response_text, from_id, to_id, message_text, my_node):
    """Replace variables in response text with actual values."""
    i = get_interface()
    
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


def check_autoresponder(message_text, from_id, to_id):
    """Check if message should trigger an auto-response."""
    global last_response_times
    
    if not autoresponder_config.get("enabled", False):
        return None
    
    # Skip null or empty messages
    if not message_text or message_text.strip() == "":
        return None
    
    # Don't respond to our own messages
    my_node = get_my_node()
    if my_node and from_id == my_node["id"]:
        return None
    
    is_broadcast = to_id == "^all"
    message_lower = message_text.lower().strip()
    
    for rule in autoresponder_config.get("rules", []):
        if not rule.get("enabled", False):
            continue
        
        # Check message type filter
        msg_type = rule.get("messageType", "both")
        if msg_type == "broadcast" and not is_broadcast:
            continue
        if msg_type == "direct" and is_broadcast:
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
        
        if not matched:
            continue
        
        # Check cooldown
        rule_id = rule.get("id", "")
        cooldown = rule.get("cooldownSeconds", 60)
        now = time.time()
        last_time = last_response_times.get(rule_id, 0)
        
        if now - last_time < cooldown:
            continue  # Still in cooldown
        
        # Update last response time
        last_response_times[rule_id] = now
        
        # Get response and substitute variables
        response = rule.get("response", "Auto-reply")
        response = substitute_variables(response, from_id, to_id, message_text, my_node)
        
        return response
    
    return None


# ── Helpers ─────────────────────────────────────────────────────────────────

def get_interface():
    """Return current interface or None."""
    global iface
    return iface


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


def get_my_node():
    """Get our own node info."""
    i = get_interface()
    if not i or not i.nodes:
        return None
    my_id = i.myInfo.my_node_num if hasattr(i, 'myInfo') and i.myInfo else None
    if my_id and my_id in i.nodesByNum:
        return get_node_info(i.nodesByNum[my_id])
    return None


# ── Packet listener ────────────────────────────────────────────────────────

def on_receive(packet, interface):
    """Called on every received packet."""
    global packet_log, traceroute_results, mqtt_health
    try:
        # Check if message should be filtered
        if should_filter_message(packet):
            return  # Skip processing this packet
        
        now = time.time()
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
        
        # Track MQTT vs RF messages for health monitoring
        # MQTT messages: no SNR/RSSI AND from a different node (not our own telemetry/routing)
        my_node = get_my_node()
        my_id = my_node["id"] if my_node else None
        from_id = packet.get("fromId", "?")
        
        has_rf_metrics = packet.get("rxSnr") is not None or packet.get("rxRssi") is not None
        is_from_other_node = from_id != my_id
        
        if not has_rf_metrics and is_from_other_node:
            # No RF metrics and from another node = MQTT
            mqtt_health["last_mqtt_message"] = now
            mqtt_health["mqtt_message_count"] += 1
        elif has_rf_metrics:
            # Has RF metrics = RF message
            mqtt_health["last_rf_message"] = now
            mqtt_health["rf_message_count"] += 1
        # Else: from our own node without RF metrics = ignore for health tracking
        # Add text if it's a text message
        decoded = packet.get("decoded", {})
        if decoded.get("portnum") == "TEXT_MESSAGE_APP":
            text = decoded.get("text", "")
            entry["text"] = text
            
            # Check for auto-responder trigger
            from_id = packet.get("fromId", "?")
            to_id = packet.get("toId", "?")
            auto_response = check_autoresponder(text, from_id, to_id)
            
            if auto_response:
                logger.info(f"Auto-responding to '{text}' from {from_id} with '{auto_response}'")
                try:
                    # Send response in a separate thread to avoid blocking
                    def send_auto_response():
                        time.sleep(2)  # Small delay to avoid immediate response
                        i = get_interface()
                        if i:
                            # Respond to broadcast as broadcast, direct as direct
                            dest = None if to_id == "^all" else hex_to_int(from_id)
                            i.sendText(auto_response, destinationId=dest, channelIndex=0)
                    
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
            
            traceroute_results[from_id] = {
                "route": route,
                "routeBack": route_back,
                "snrTowards": snr_towards,
                "snrBack": snr_back,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "status": "complete"
            }
            
            logger.info(f"Updated traceroute_results[{from_id}] to complete")

        packet_log.append(entry)
        if len(packet_log) > PACKET_LOG_MAX:
            packet_log = packet_log[-PACKET_LOG_MAX:]
    except Exception as e:
        logger.warning(f"Error processing packet: {e}")


def on_traceroute(packet, interface):
    """Handle traceroute responses for the sweep feature."""
    global traceroute_results
    try:
        decoded = packet.get("decoded", {})
        if decoded.get("portnum") == "TRACEROUTE_APP":
            tr = decoded.get("traceroute", {})
            from_id = packet.get("fromId", "?")
            route = tr.get("route", [])
            route_back = tr.get("routeBack", [])
            snr_towards = tr.get("snrTowards", [])
            snr_back = tr.get("snrBack", [])
            traceroute_results[from_id] = {
                "route": route,
                "routeBack": route_back,
                "snrTowards": snr_towards,
                "snrBack": snr_back,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "status": "complete"
            }
    except Exception as e:
        logger.warning(f"Error processing traceroute: {e}")


# ── Routes ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/connect", methods=["POST"])
def connect():
    """Connect to a Meshtastic node via TCP."""
    global iface, mqtt_health
    data = request.json or {}
    host = data.get("host", "192.168.1.134")
    port = int(data.get("port", 4403))

    with iface_lock:
        # Close existing connection
        if iface:
            try:
                iface.close()
            except:
                pass
            iface = None

        try:
            pub.subscribe(on_receive, "meshtastic.receive")
            iface = meshtastic.tcp_interface.TCPInterface(
                hostname=host, portNumber=port
            )
            mqtt_health["connection_start"] = time.time()
            time.sleep(2)  # Wait for node DB to populate

            my_node = get_my_node()
            node_count = len(iface.nodes) if iface.nodes else 0

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
    global iface
    with iface_lock:
        if iface:
            try:
                iface.close()
            except:
                pass
            iface = None
    return jsonify({"status": "disconnected"})


@app.route("/api/status")
def status():
    """Get connection status and basic info."""
    i = get_interface()
    if not i:
        return jsonify({"connected": False})
    try:
        my_node = get_my_node()
        return jsonify({
            "connected": True,
            "myNode": my_node,
            "nodeCount": len(i.nodes) if i.nodes else 0
        })
    except:
        return jsonify({"connected": False})


@app.route("/api/nodes")
def nodes():
    """Get all nodes with optional filtering."""
    i = get_interface()
    if not i or not i.nodes:
        return jsonify([])

    filter_type = request.args.get("filter", "all")  # all, rf, mqtt, local
    my_node = get_my_node()
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
    global traceroute_results
    i = get_interface()
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
        my_node = get_my_node()
        if my_node and (dest_hex == my_node["id"] or dest_int == my_node["num"]):
            return jsonify({"error": "Cannot traceroute to yourself"}), 400
        
        # Check if there's already a pending/recent traceroute to this node (within 30 seconds)
        if dest_hex in traceroute_results:
            existing = traceroute_results[dest_hex]
            if existing.get("status") == "pending":
                return jsonify({"error": "Traceroute already in progress for this node"}), 400
            # Check timestamp to prevent spam
            try:
                last_time = datetime.fromisoformat(existing.get("timestamp", ""))
                if (datetime.now(timezone.utc) - last_time).total_seconds() < 30:
                    return jsonify({"error": "Please wait 30 seconds between traceroutes to the same node"}), 400
            except:
                pass
        
        traceroute_results[dest_hex] = {
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
                    if dest_hex in traceroute_results and traceroute_results[dest_hex].get("status") == "pending":
                        logger.warning(f"Traceroute to {dest_hex} timed out")
                        traceroute_results[dest_hex] = {
                            "status": "timeout",
                            "error": "No response received within 60 seconds",
                            "timestamp": datetime.now(timezone.utc).isoformat()
                        }
                
                timeout_thread = threading.Thread(target=check_timeout, daemon=True)
                timeout_thread.start()
                
            except Exception as e:
                logger.error(f"Error sending traceroute to {dest_hex}: {e}")
                traceroute_results[dest_hex] = {
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
    return jsonify(traceroute_results)


@app.route("/api/traceroute/cancel", methods=["POST"])
def cancel_traceroute():
    """Cancel a pending traceroute."""
    global traceroute_results
    data = request.json or {}
    node_id = data.get("nodeId")
    
    if not node_id:
        return jsonify({"error": "No nodeId specified"}), 400
    
    if node_id in traceroute_results and traceroute_results[node_id].get("status") == "pending":
        traceroute_results[node_id] = {
            "status": "cancelled",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "command": traceroute_results[node_id].get("command", "")
        }
        return jsonify({"status": "cancelled", "nodeId": node_id})
    
    return jsonify({"error": "No pending traceroute found for this node"}), 404


@app.route("/api/sweep", methods=["POST"])
def sweep():
    """Automated RF sweep - traceroute all non-MQTT nodes to map RF reach."""
    global sweep_status
    i = get_interface()
    if not i or not i.nodes:
        return jsonify({"error": "Not connected"}), 400

    if sweep_status["running"]:
        return jsonify({"error": "Sweep already running"}), 400

    # Get non-MQTT nodes (actual RF-reachable candidates)
    candidates = []
    for node_id, node in i.nodes.items():
        info = get_node_info(node)
        # Skip MQTT-only nodes and ourselves
        my_node = get_my_node()
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

    sweep_status = {
        "running": True,
        "current": None,
        "total": len(candidates),
        "completed": 0,
        "results": {}
    }

    def run_sweep():
        global sweep_status, traceroute_results
        try:
            for idx, node_info in enumerate(candidates):
                # Check if cancelled before starting each node
                if not sweep_status["running"]:
                    logger.info(f"Sweep cancelled at {idx}/{len(candidates)}")
                    break

                dest_id = node_info["id"]
                sweep_status["current"] = dest_id
                sweep_status["completed"] = idx  # Update progress before starting

                traceroute_results[dest_id] = {
                    "status": "pending",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "command": f"sendTraceRoute({dest_id}, hopLimit=6)"
                }

                try:
                    dest_int = hex_to_int(dest_id)
                    i.sendTraceRoute(dest_int, hopLimit=6, channelIndex=0)
                    
                    # Wait for response (traceroutes can take a while)
                    wait_time = 0
                    while wait_time < 30:  # 30 second timeout per node
                        # Check for cancellation during wait
                        if not sweep_status["running"]:
                            traceroute_results[dest_id] = {
                                "status": "cancelled",
                                "timestamp": datetime.now(timezone.utc).isoformat()
                            }
                            break
                        
                        time.sleep(1)
                        wait_time += 1
                        
                        if dest_id in traceroute_results and traceroute_results[dest_id].get("status") == "complete":
                            break

                    # Only mark as timeout if not cancelled and not complete
                    if sweep_status["running"] and traceroute_results.get(dest_id, {}).get("status") != "complete":
                        traceroute_results[dest_id] = {
                            "status": "timeout",
                            "timestamp": datetime.now(timezone.utc).isoformat()
                        }

                except Exception as e:
                    traceroute_results[dest_id] = {
                        "status": "error",
                        "error": str(e),
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    }

                sweep_status["results"][dest_id] = traceroute_results.get(dest_id, {})
                
                # Check if cancelled before delay
                if not sweep_status["running"]:
                    break
                    
                # Small delay between traceroutes to not overwhelm the mesh
                time.sleep(3)

            # Mark final completion count
            sweep_status["completed"] = len(candidates)
            
        finally:
            sweep_status["running"] = False
            sweep_status["current"] = None
            logger.info(f"Sweep finished: {sweep_status['completed']}/{sweep_status['total']} nodes tested")

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
    return jsonify(sweep_status)


@app.route("/api/sweep/stop", methods=["POST"])
def sweep_stop():
    """Stop a running sweep."""
    global sweep_status
    sweep_status["running"] = False
    return jsonify({"status": "stopped"})


@app.route("/api/send", methods=["POST"])
def send_message():
    """Send a text message to the mesh or a specific node."""
    global packet_log
    i = get_interface()
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
            mqtt_health["mqtt_sent_count"] += 1
        
        # Add outgoing message to packet log
        my_node = get_my_node()
        my_id = my_node["id"] if my_node else "local"
        
        packet_log.append({
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
        
        if len(packet_log) > PACKET_LOG_MAX:
            packet_log = packet_log[-PACKET_LOG_MAX:]

        return jsonify({"status": "sent"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/packets")
def packets():
    """Get recent packet log."""
    since = request.args.get("since")
    if since:
        filtered = [p for p in packet_log if p["time"] > since]
        return jsonify(filtered)
    return jsonify(packet_log[-100:])  # Last 100 by default


@app.route("/api/device/config")
def device_config():
    """Get device configuration (localConfig, moduleConfig, channels)."""
    i = get_interface()
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
    i = get_interface()
    if not i or not i.nodes:
        return jsonify({"error": "Not connected"}), 400

    if node_id in i.nodes:
        node = i.nodes[node_id]
        info = get_node_info(node)

        # Add traceroute result if available
        info["traceroute"] = traceroute_results.get(node_id)

        # Calculate distance
        my_node = get_my_node()
        if my_node and my_node["lat"] and my_node["lon"] and info["lat"] and info["lon"]:
            info["distance_km"] = round(
                haversine(my_node["lat"], my_node["lon"], info["lat"], info["lon"]), 2
            )

        return jsonify(info)

    return jsonify({"error": "Node not found"}), 404


@app.route("/api/mqtt/health", methods=["GET"])
def get_mqtt_health():
    """Get MQTT health status and metrics."""
    global mqtt_health
    
    now = time.time()
    
    # Calculate time since last messages
    last_mqtt_ago = (now - mqtt_health["last_mqtt_message"]) if mqtt_health["last_mqtt_message"] else None
    last_rf_ago = (now - mqtt_health["last_rf_message"]) if mqtt_health["last_rf_message"] else None
    uptime = (now - mqtt_health["connection_start"]) if mqtt_health["connection_start"] else 0
    
    # Determine health status
    health_status = "unknown"
    health_color = "gray"
    health_message = "No data yet"
    
    if mqtt_health["last_mqtt_message"]:
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
    i = get_interface()
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
        "mqtt_count": mqtt_health["mqtt_message_count"],
        "rf_count": mqtt_health["rf_message_count"],
        "mqtt_sent": mqtt_health["mqtt_sent_count"],
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


@app.route("/api/device/mqtt/config", methods=["GET"])
def get_device_mqtt_config():
    """Get current MQTT configuration from the connected device."""
    i = get_interface()
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
    i = get_interface()
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
    i = get_interface()
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
    i = get_interface()
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
    i = get_interface()
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


# ── Main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Load auto-responder config on startup
    load_autoresponder_config()
    
    print("=" * 60)
    print("  Meshtastic Dashboard")
    print("  Open http://localhost:5000 in your browser")
    print(f"  Auto-responder: {'ENABLED' if autoresponder_config.get('enabled') else 'DISABLED'} ({len(autoresponder_config.get('rules', []))} rules)")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=True, threaded=True)
