#!/bin/bash
# View messages from the persistent message database
# Usage: ./view_messages.sh [session_name] [limit]

DB_FILE="messages.db"
SESSION_NAME="${1:-}"
LIMIT="${2:-50}"

if [ ! -f "$DB_FILE" ]; then
    echo "Error: Database file '$DB_FILE' not found"
    exit 1
fi

if [ -z "$SESSION_NAME" ]; then
    # Show all sessions with message counts
    echo "Available sessions:"
    echo "=================="
    sqlite3 "$DB_FILE" "SELECT session_name, COUNT(*) as msg_count FROM messages GROUP BY session_name;" | \
        awk -F'|' '{printf "%-20s %s messages\n", $1, $2}'
    echo ""
    echo "Usage: $0 <session_name> [limit]"
    echo "Example: $0 3bfc 20"
else
    # Show messages for specific session
    echo "Messages for session: $SESSION_NAME (last $LIMIT)"
    echo "========================================================================"
    sqlite3 "$DB_FILE" <<EOF
.mode column
.headers off
SELECT 
    session_name || ' | ' || 
    datetime(substr(timestamp, 1, 19)) || ' | ' || 
    COALESCE(from_id, 'unknown') || ' -> ' || 
    COALESCE(to_id, 'unknown') || ' | ' || 
    COALESCE(substr(text, 1, 80), '[no text]')
FROM messages 
WHERE session_name = '$SESSION_NAME'
ORDER BY created_at ASC 
LIMIT $LIMIT;
EOF
fi
