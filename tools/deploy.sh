#!/bin/sh
# Copy the server to the container and restart it, in one step.
#
# The container exists for one reason: the device is configured with its address
# and changing that needs someone to press buttons on the device itself. Every
# other part of the work -- writing code, running the tests, reasoning about the
# protocol -- happens on the machine this is run from. This script is the whole
# of the container's involvement, so that editing a file and seeing the effect
# is one command rather than four round trips with hand-written quoting.
#
#     tools/deploy.sh            # sync and restart
#     tools/deploy.sh --logs     # sync, restart, then follow the log
set -eu

root="$(cd -- "$(dirname -- "$0")/.." && pwd)"
host="${TV_DEPLOY_HOST:-root@192.168.0.114}"
password_file="${TV_DEPLOY_PW:-$HOME/.tv_deploy_pw}"
remote=/opt/tv-server-src

command -v sshpass >/dev/null 2>&1 || { echo "sshpass is required" >&2; exit 1; }
[ -f "$password_file" ] || { echo "no password file at $password_file" >&2; exit 1; }

# The whole of server/ and the tools the container runs, and nothing else: the
# firmware, the docs and the test suite are not used there.
sshpass -f "$password_file" ssh -o StrictHostKeyChecking=no "$host" \
    "rm -rf $remote/server/__pycache__ $remote/tools/__pycache__" >/dev/null 2>&1 || true
sshpass -f "$password_file" scp -q -o StrictHostKeyChecking=no -r \
    "$root/server" "$root/tools" "$host:$remote/"
sshpass -f "$password_file" scp -q -o StrictHostKeyChecking=no \
    "$root/channels.txt" "$host:/opt/tv-server-data/channels.txt"

sshpass -f "$password_file" ssh -o StrictHostKeyChecking=no "$host" \
    "systemctl restart tv-server && sleep 2 && systemctl is-active tv-server"

if [ "${1:-}" = "--logs" ]; then
    exec sshpass -f "$password_file" ssh -o StrictHostKeyChecking=no "$host" \
        "journalctl -u tv-server -f -n 20 --no-pager"
fi
