#!/usr/bin/env bash
# Prime Agent daemon watchdog — ensures the background daemon stays alive.
# Called by systemd (prime-agent-watchdog.service) on a 30s interval.
#
# `prime-agent status` auto-spawns the daemon if it's down, so simply calling
# it is enough to keep the daemon alive. This script also cleans up stale
# sockets and verifies health after.
set -euo pipefail

PRIME_AGENT="/home/user/.hermes/node/bin/prime-agent"
SOCKET="/tmp/prime-agent-1000/daemon.sock"

# Clean up stale socket files before probing (orphan .sock with no process).
if [ -S "$SOCKET" ] && ! pgrep -f "prime-agent" >/dev/null 2>&1; then
    rm -f "$SOCKET" 2>/dev/null || true
fi

# `prime-agent status` auto-spawns the daemon if down. This is the
# self-healing mechanism — calling it on a timer keeps the daemon alive.
STATUS_OUTPUT="$("$PRIME_AGENT" status 2>&1 || true)"

# Verify the daemon is healthy: must contain "current" with a valid PID.
if echo "$STATUS_OUTPUT" | grep -qE 'current\s+[0-9]+\s+'; then
    exit 0
fi

# If still not healthy, try a direct spawn query as fallback.
"$PRIME_AGENT" --offline -p --no-tools --no-extensions --no-skills --no-context-files "ok" >/dev/null 2>&1 || true

# Final check with retries.
for _ in 1 2 3; do
    sleep 3
    STATUS_OUTPUT="$("$PRIME_AGENT" status 2>&1 || true)"
    if echo "$STATUS_OUTPUT" | grep -qE 'current\s+[0-9]+\s+'; then
        echo "$(date -u +%FT%TZ) prime-agent daemon restarted successfully"
        exit 0
    fi
done

echo "$(date -u +%FT%TZ) prime-agent daemon failed to start" >&2
echo "$STATUS_OUTPUT" >&2
exit 1
