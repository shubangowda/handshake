#!/bin/sh
# api-entrypoint.sh: hand the /data volume to the unprivileged `app` user,
# then run the API as `app`.
#
# Why this exists: Fly mounts a volume owned by root, and the API (which is
# not root) must write the SQLite database and each user's private Stripe
# Link login under it. Running the whole API as root would work but would
# give every child process (including the Link CLI) root. Instead this
# script runs as root for two commands and then drops privileges for good.
#
# It only changes the mount point itself (not recursively): everything below
# it is created by `app`, with the 0700 modes payments.py sets, so nothing
# else ever needs fixing. Run without root (e.g. `docker run --user`), it
# just runs the command.
set -eu

DATA_DIR="${HANDSHAKE_DATA_DIR:-/data}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR"
    chown app:app "$DATA_DIR"
    chmod 0700 "$DATA_DIR"
    # setpriv (util-linux, already in the base image) switches uid, gid and
    # groups and clears capabilities, without a setuid helper like sudo/gosu.
    # HOME is set explicitly because the environment is otherwise kept as-is.
    export HOME=/home/app
    exec setpriv --reuid=app --regid=app --init-groups --inh-caps=-all -- "$@"
fi

exec "$@"
