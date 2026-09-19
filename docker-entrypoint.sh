#!/bin/sh
# Container entrypoint (see the Dockerfile). Two jobs, then exec the command as `salescoach`:
#   1. make the data volume writable by the app user: platforms mount a fresh volume owned by root;
#   2. drop root. `setpriv` is in util-linux, which python:slim ships.
# If the container is already running as a non-root user (the platform chose one), there is nothing
# to chown and nothing to drop: exec straight away.
set -eu

DATA="${SALESCOACH_DATA:-/data}"
RUNTIME="${SALESCOACH_RUNTIME:-$DATA/runtime}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA" "$RUNTIME"
    # Only the top-level folders: a large volume must not be walked on every start. The app creates
    # everything below them itself, as salescoach.
    chown salescoach:salescoach "$DATA" "$RUNTIME"
    # setpriv keeps the environment, and HOME=/root would make every "~" path in the app (the optional
    # Claude CLI, the optional Jarvis store) a permission error instead of simply absent.
    export HOME=/home/salescoach
    exec setpriv --reuid=salescoach --regid=salescoach --init-groups "$@"
fi
exec "$@"
