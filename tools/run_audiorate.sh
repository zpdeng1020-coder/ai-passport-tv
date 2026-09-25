#!/bin/sh
# Run the audio-rate ramp on the container and show its output.
#
# A script rather than an inline command: the pattern used to stop the previous
# run appears in any command line that names it, so `pkill -f` written inline
# kills the shell running it, and the nested quoting needed over ssh kept
# mangling the arguments.
set -u

log=/tmp/ar-run.log

for pid in $(pgrep -f "au[d]iorate" ; pgrep -f "[a]r\.py"); do
    [ "$pid" != "$$" ] && kill -9 "$pid" 2>/dev/null
done
sleep 1

cd /opt/tv-server-src || exit 1
nohup python3 -u /root/ar.py > "$log" 2>&1 &

sleep 8
echo "--- listening? ---"
ss -lntp 2>/dev/null | grep 8096 || echo "not listening"
echo "--- log ---"
cat "$log"
