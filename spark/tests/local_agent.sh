#!/usr/bin/env bash
# Start a local net-snmp agent for the live collector tests.
#
# Listens on 127.0.0.1:11161 with community "sparktest", so it never collides
# with a real agent on 161 and never leaves the loopback interface.
#
#   ./tests/local_agent.sh start
#   pytest -q
#   ./tests/local_agent.sh stop

set -euo pipefail

CONF=/tmp/spark-test-snmpd.conf
PID=/tmp/spark-test-snmpd.pid
LOG=/tmp/spark-test-snmpd.log
PORT=11161

case "${1:-start}" in
  start)
    command -v snmpd >/dev/null 2>&1 || {
      echo "snmpd not found. Install it:  apt-get install snmpd  |  brew install net-snmp"
      exit 1
    }
    cat > "$CONF" <<EOF
agentaddress udp:127.0.0.1:$PORT
rocommunity sparktest 127.0.0.1
sysLocation Test Rack
sysContact spark@example.com
sysName test-switch-01
EOF
    snmpd -C -c "$CONF" -Lf "$LOG" -p "$PID"
    sleep 1
    echo "Agent listening on 127.0.0.1:$PORT (community: sparktest)"
    ;;
  stop)
    [ -f "$PID" ] && kill "$(cat "$PID")" 2>/dev/null && echo "Stopped" || echo "Not running"
    ;;
  *)
    echo "Usage: $0 {start|stop}"
    exit 1
    ;;
esac
