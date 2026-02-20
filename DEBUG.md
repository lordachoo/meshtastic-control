# Debug Logging

The dashboard includes a debug logging system that writes detailed debug information to a file instead of cluttering the console output.

## Enabling Debug Mode

Set the `DEBUG` environment variable to enable debug logging:

```bash
# Enable debug mode
DEBUG=true python app_multiuser.py

# Or with custom log file location
DEBUG=true DEBUG_LOG_FILE=my_debug.log python app_multiuser.py
```

## Default Settings

- **Debug enabled:** `false` (disabled by default)
- **Debug log file:** `debug.log` (in the current directory)

## What Gets Logged

When debug mode is enabled, the following information is logged to the debug file:

- **[SESSION]** - Session creation, lookup, auto-reconnect events
- **[BEACON]** - Beacon thread lifecycle, message sending, countdowns
- **[AUTO-RESPONDER]** - Rule evaluation, trigger matching, cooldown checks
- **[PACKET]** - Incoming packet details, routing information
- **[STORE&FORWARD]** - Module status checks, configuration details

## Viewing Debug Logs

```bash
# View the entire log
cat debug.log

# Follow the log in real-time
tail -f debug.log

# Search for specific events
grep "\[AUTO-RESPONDER\]" debug.log
grep "\[BEACON\]" debug.log
grep "\[PACKET\]" debug.log

# View recent beacon activity
grep "\[BEACON\]" debug.log | tail -20

# Check auto-responder rule evaluation
grep "Rule.*matched" debug.log
```

## Clearing Debug Logs

```bash
# Clear the debug log
> debug.log

# Or delete it
rm debug.log
```

## Production Use

For production deployments, keep debug mode **disabled** to avoid excessive disk usage. The regular application logs (via `logger`) will still capture important events and errors.

## Troubleshooting

If debug logs aren't appearing:

1. Check that `DEBUG=true` is set when starting the app
2. Verify the debug log file path is writable
3. Check file permissions on the debug log file
4. Look for any startup errors in the console

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DEBUG` | `false` | Enable/disable debug logging (`true`, `1`, `yes` to enable) |
| `DEBUG_LOG_FILE` | `debug.log` | Path to the debug log file |
