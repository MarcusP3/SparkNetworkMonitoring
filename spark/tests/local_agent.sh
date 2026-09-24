#!/usr/bin/env bash
# Start a local net-snmp agent for the live collector tests.
#
# Listens on 127.0.0.1:11161 with community "sparktest", so it never collides
# with a real agent on 161 and never leaves the loopback interface.
#
# Also answers SNMPv3 as user "sparkv3" (SHA auth, AES privacy) -- the mode a
# network worth protecting actually uses, and the one that was broken for as
# long as nothing tested it: pysnmp needs the `cryptography` package for v3
# privacy and disables encryption silently without it.
#
#   ./tests/local_agent.sh start
#   pytest -q
#   ./tests/local_agent.sh stop

set -euo pipefail

CONF=/tmp/spark-test-snmpd.conf
PID=/tmp/spark-test-snmpd.pid
LOG=/tmp/spark-test-snmpd.log
# createUser is consumed at startup and the localized keys written here. A
# throwaway directory, so a test run never touches the system's own agent state.
STATE=/tmp/spark-test-snmpd-state
PORT=11161

case "${1:-start}" in
  start)
    command -v snmpd >/dev/null 2>&1 || {
      echo "snmpd not found. Install it:  apt-get install snmpd  |  brew install net-snmp"
      exit 1
    }
    rm -rf "$STATE" && mkdir -p "$STATE"
    cat > "$CONF" <<EOF
agentaddress udp:127.0.0.1:$PORT
rocommunity sparktest 127.0.0.1
createUser sparkv3 SHA "sparktest-auth" AES "sparktest-priv"
rouser sparkv3 priv
createUser sparkauth SHA "sparktest-auth"
rouser sparkauth auth
sysLocation Test Rack
sysContact spark@example.com
sysName test-switch-01
EOF
    SNMP_PERSISTENT_DIR="$STATE" snmpd -C -c "$CONF" -Lf "$LOG" -p "$PID"
    sleep 1
    echo "Agent listening on 127.0.0.1:$PORT"
    echo "  v2c community: sparktest"
    echo "  v3 user: sparkv3    authPriv  SHA/sparktest-auth  AES/sparktest-priv"
    echo "  v3 user: sparkauth  authNoPriv  SHA/sparktest-auth"
    ;;
  stop)
    [ -f "$PID" ] && kill "$(cat "$PID")" 2>/dev/null && echo "Stopped" || echo "Not running"
    ;;
  *)
    echo "Usage: $0 {start|stop}"
    exit 1
    ;;
esac
