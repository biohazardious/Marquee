#!/bin/sh
# linuxserver.io-style PUID/PGID handling: the container writes into volumes the host
# owns, so it has to become the host's user rather than root.
set -e

if [ "$(id -u)" = "0" ] && [ -n "$PUID" ]; then
    if ! getent group marquee >/dev/null 2>&1; then
        addgroup --gid "$PGID" marquee 2>/dev/null || true
    fi
    if ! getent passwd marquee >/dev/null 2>&1; then
        adduser --uid "$PUID" --gid "$PGID" --disabled-password --gecos "" \
                --no-create-home marquee 2>/dev/null || true
    fi
    mkdir -p "$XDG_CACHE_HOME" 2>/dev/null || true
    for directory in /config "$XDG_CACHE_HOME" /downloads /library; do
        [ -d "$directory" ] && chown "$PUID:$PGID" "$directory" 2>/dev/null || true
    done
    exec setpriv --reuid "$PUID" --regid "$PGID" --init-groups \
        marquee --web --host "$MARQUEE_HOST" --port "$MARQUEE_PORT" \
        --config "$MARQUEE_CONFIG" "$@"
fi

exec marquee --web --host "$MARQUEE_HOST" --port "$MARQUEE_PORT" \
    --config "$MARQUEE_CONFIG" "$@"
