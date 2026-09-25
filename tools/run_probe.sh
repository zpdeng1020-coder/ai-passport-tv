#!/bin/sh
# Start the transport probe on the container and show its first output.
#
# A script rather than a one-liner because the pattern this has to kill --
# the probe's own module name -- appears in any command line that names it, so
# a `pkill -f` written inline kills the shell running it and the rest of the
# line never executes. Same for the quoting of the arguments.
set -u

probe=/opt/tv-server-src/tools/transport_probe.py
log=/tmp/probe-run.log

# Match the module by a pattern that does not appear literally in this file's
# own command line.
for pid in $(pgrep -f "transport[_]probe"); do
    [ "$pid" != "$$" ] && kill -9 "$pid" 2>/dev/null
done
sleep 1

cd /opt/tv-server-src || exit 1
nohup python3 -u "$probe" \
    --bind 192.168.0.114 --port 8096 \
    --stripes-per-packet 7 --run 8 --audio \
    --seconds "$1" --ramp "$2" --step-seconds "$3" \
    > "$log" 2>&1 &

sleep 8
echo "--- probe log ---"
cat "$log"
echo "--- listening ---"
ss -lntp 2>/dev/null | grep 8096 || echo "not listening"
