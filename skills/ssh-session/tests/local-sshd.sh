#!/usr/bin/env bash
# Bring up a throwaway sshd on 127.0.0.1 so sshsess can be tested against a
# real SSH server without touching any remote machine.
#
# Everything lives in one work directory: its own host key, its own client key,
# its own known_hosts, its own sshd_config. Nothing system-wide is installed or
# enabled, no sudo is needed, ~/.ssh is never modified, and the listener is
# bound to loopback so the box is not exposed. `stop` removes all of it.
#
#   ./local-sshd.sh start   -> starts sshd, prints the connect target
#   ./local-sshd.sh stop    -> kills it and deletes the work dir
#   ./local-sshd.sh status
#
# Why an ssh_config with a Host alias instead of passing -p/-i on the command
# line: the agent shell is zsh, which does NOT word-split unquoted variables,
# so `-- $ARGS` arrives as one mangled argument ("Bad port ..."). One -F flag
# sidesteps that entirely.
set -euo pipefail

# `ss` is Linux-only and `netstat` output differs per OS, so ask python3 (which
# the skill already requires) whether the port accepts connections.
listening() {
  python3 - "$1" <<'PY'
import socket, sys
s = socket.socket()
s.settimeout(0.3)
sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PY
}

DRIVER="$(cd "$(dirname "${BASH_SOURCE[0]}")/../scripts" && pwd)/sshsess.py"
W="${SSHSESS_TEST_DIR:-/tmp/sshsess-test}"
PORT="${SSHSESS_TEST_PORT:-2222}"
ALIAS=local-test

start() {
  if [ -f "$W/sshd.pid" ] && kill -0 "$(cat "$W/sshd.pid")" 2>/dev/null; then
    echo "already running (pid $(cat "$W/sshd.pid")), work dir $W"
    print_usage_block
    return 0
  fi
  # sshd normally is not in PATH (it lives in sbin), and the path differs per
  # OS: /usr/sbin on macOS and Debian, /usr/bin on Arch.
  SSHD="${SSHD:-}"
  if [ -z "$SSHD" ]; then
    for cand in "$(command -v sshd 2>/dev/null)" /usr/sbin/sshd /usr/bin/sshd \
                /usr/local/sbin/sshd /opt/homebrew/sbin/sshd; do
      [ -n "$cand" ] && [ -x "$cand" ] && { SSHD="$cand"; break; }
    done
  fi
  [ -n "$SSHD" ] || { echo "sshd binary not found; install openssh" >&2; exit 1; }

  rm -rf "$W"; mkdir -p "$W"; chmod 700 "$W"
  ssh-keygen -q -t ed25519 -f "$W/hostkey" -N '' -C sshsess-test-host
  ssh-keygen -q -t ed25519 -f "$W/id_test" -N '' -C sshsess-test-client
  cp "$W/id_test.pub" "$W/authorized_keys"
  chmod 600 "$W/authorized_keys" "$W/hostkey" "$W/id_test"

  cat > "$W/sshd_config" <<EOF
Port $PORT
ListenAddress 127.0.0.1
HostKey $W/hostkey
PidFile $W/sshd.pid
AuthorizedKeysFile $W/authorized_keys
StrictModes no
UsePAM no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
PrintMotd no
LogLevel VERBOSE
EOF

  # Host alias so test commands stay short and quoting-proof.
  cat > "$W/ssh_config" <<EOF
Host $ALIAS
    HostName 127.0.0.1
    Port $PORT
    User $(id -un)
    IdentityFile $W/id_test
    IdentitiesOnly yes
    UserKnownHostsFile $W/known_hosts
    StrictHostKeyChecking accept-new
EOF

  # No -D and no '&': let sshd daemonize itself. Backgrounding a foreground
  # sshd from this script leaves it in our process group, where it dies (or
  # takes the script's exit code with it) as soon as the script finishes.
  "$SSHD" -f "$W/sshd_config" -E "$W/sshd.log"
  for _ in $(seq 1 40); do
    listening "$PORT" && break
    sleep 0.1
  done
  listening "$PORT" || {
    echo "sshd failed to listen; log:" >&2; cat "$W/sshd.log" >&2; exit 1; }

  # Prove the server actually accepts us before handing it to the caller.
  ssh -F "$W/ssh_config" "$ALIAS" 'true' >/dev/null 2>&1 || {
    echo "sshd is listening but authentication failed; log:" >&2
    tail -20 "$W/sshd.log" >&2; exit 1; }

  echo "sshd up on 127.0.0.1:$PORT (loopback only), work dir $W"
  print_usage_block
}

print_usage_block() {
  cat <<EOF

Use it with sshsess like this (the login shell here may not be POSIX, so most
cases need --shell):

  S=$DRIVER
  \$S new NAME $ALIAS --shell 'bash -i' -- -F $W/ssh_config
EOF
}

stop() {
  if [ -f "$W/sshd.pid" ]; then
    kill "$(cat "$W/sshd.pid")" 2>/dev/null || true
    sleep 0.3
  fi
  pkill -f "sshd -f $W/sshd_config" 2>/dev/null || true
  rm -rf "$W"
  echo "stopped, $W removed"
}

status() {
  if [ -f "$W/sshd.pid" ] && kill -0 "$(cat "$W/sshd.pid")" 2>/dev/null; then
    echo "running: pid $(cat "$W/sshd.pid") on 127.0.0.1:$PORT, work dir $W"
  else
    echo "not running"
  fi
}

case "${1:-start}" in
  start) start ;;
  stop) stop ;;
  status) status ;;
  *) echo "usage: $0 {start|stop|status}" >&2; exit 2 ;;
esac
