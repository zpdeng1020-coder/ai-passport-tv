#!/bin/sh
# Start the live server on the container with a chosen media filter.
#
# A script rather than a command line for two reasons. The pattern used to stop
# a previous run -- the module name -- appears in any command line that names
# it, so `pkill -f` written inline kills the shell running it and the rest of
# the line never executes; the bracket around one character keeps the pattern
# from matching this file's own command line. And the two environment variables
# below are easy to omit, which fails with a message that does not mention them:
# without TV_DATA_DIR the channel list is looked for beside the code, where it
# is not, and the program reports only "ffmpeg missing or bad channel list".
#
#     tools/start_live.sh audio ch013 /tmp/audio-only.log
#     tools/start_live.sh video ch013 /tmp/video-only.log
#     tools/start_live.sh both  ch013 /tmp/both.log
set -u

filter="$1"
channel="$2"
log="$3"

for pid in $(pgrep -f "tv_serve[r]"); do
    [ "$pid" != "$$" ] && kill -9 "$pid" 2>/dev/null
done
pkill -f "launch[.]py" 2>/dev/null
sleep 2

export TV_DATA_DIR=/opt/tv-server-data
export PYTHONPATH=/opt/tv-server-src
cd /opt/tv-server-src || exit 1
exec python3 -u -m server.tv_server live \
    --bind 192.168.0.114 --port 8096 --media "$filter" --channel "$channel" \
    > "$log" 2>&1 < /dev/null
