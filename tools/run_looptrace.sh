#!/bin/sh
# Run the loop tracer on the container in place of the server, and print its
# summary once the device has had a session.
#
# A script rather than an inline command: the pattern used to stop the previous
# run appears in any command line that names it, so `pkill -f` written inline
# kills the shell running it, and nested quoting over ssh kept mangling the
# arguments.
set -u

seconds="${1:-30}"
log=/tmp/looptrace.log
result=/tmp/looptrace.out

# Free the port without killing this script: the bracket keeps the pattern from
# matching the command line that contains it.
for pid in $(pgrep -f "loo[p]_trace"); do
    [ "$pid" != "$$" ] && kill -9 "$pid" 2>/dev/null
done
systemctl stop tv-server 2>/dev/null
sleep 2

cd /opt/tv-server-src || exit 1
nohup python3 -u tools/loop_trace.py \
    --bind 192.168.0.114 --port 8096 --channel ch000 --seconds "$seconds" \
    > "$log" 2>&1 &

sleep 10
echo "--- listening ---"
ss -lntp 2>/dev/null | grep 8096 || echo "NOT LISTENING"
echo "--- so far ---"
tail -20 "$log"
