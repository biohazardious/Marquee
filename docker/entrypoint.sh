#!/bin/sh
# linuxserver.io-style PUID/PGID handling: the container writes into volumes the host
# owns, so it has to become the host's user rather than root.
set -e

# The port inside the container. MARQUEE_PORT is the documented name; PORT is what
# most app templates (and this project's own .env) call it, so it counts too.
MARQUEE_PORT="${MARQUEE_PORT:-${PORT:-8585}}"
export MARQUEE_PORT

if [ "$(id -u)" = "0" ] && [ -n "$PUID" ]; then
    if ! getent group marquee >/dev/null 2>&1; then
        addgroup --gid "$PGID" marquee 2>/dev/null || true
    fi
    if ! getent passwd marquee >/dev/null 2>&1; then
        adduser --uid "$PUID" --gid "$PGID" --disabled-password --gecos "" \
                --no-create-home marquee 2>/dev/null || true
    fi
    mkdir -p "$XDG_CACHE_HOME" 2>/dev/null || true
    # Only what is ours. /downloads and /library are shared with the download
    # client and the console: taking ownership of the torrent folder's root left
    # qBittorrent unable to create a folder in it, and every download errored on
    # the spot with "Permission denied". Run both containers as the same PUID/PGID
    # instead -- they write to the same files.
    for directory in /config "$XDG_CACHE_HOME"; do
        [ -d "$directory" ] && chown "$PUID:$PGID" "$directory" 2>/dev/null || true
    done
    exec setpriv --reuid "$PUID" --regid "$PGID" --init-groups \
        marquee --web --host "$MARQUEE_HOST" --port "$MARQUEE_PORT" \
        --config "$MARQUEE_CONFIG" "$@"
fi

exec marquee --web --host "$MARQUEE_HOST" --port "$MARQUEE_PORT" \
    --config "$MARQUEE_CONFIG" "$@"
