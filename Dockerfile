# Marquee — a MAME library manager in the shape of Sonarr/Radarr.
#
# Single process, no build step for the front end, no database server. The image is
# python:slim plus the package; everything that varies lives in the volumes.
FROM python:3.13-slim

LABEL org.opencontainers.image.title="Marquee" \
      org.opencontainers.image.description="Categorise, acquire and update a MAME romset" \
      org.opencontainers.image.source="https://github.com/biohazardious/Marquee" \
      org.opencontainers.image.licenses="GPL-3.0-or-later"

# tini reaps the process; certificates are for the GitHub and Pleasuredome fetches.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# The container drops to PUID, whose home is still /root -- so anything that lands in
# ~/.cache is unwritable. The cache belongs in the volume anyway: that is what makes a
# restart cheap, because the 90 MB MAME XML and the catlists survive it.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/config \
    XDG_CACHE_HOME=/config/cache \
    MARQUEE_CONFIG=/config \
    MARQUEE_HOST=0.0.0.0 \
    MARQUEE_PORT=8585 \
    PUID=1000 \
    PGID=1000

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY marquee ./marquee
RUN pip install --no-cache-dir .

# /config  settings, API key, cached XML/catlist/datfiles
# /downloads  the torrent folder, as this container sees it
# /library  the categorised output, when it is a local path
VOLUME ["/config", "/downloads", "/library"]
EXPOSE 8585

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,os;\
urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('MARQUEE_PORT','8585')+'/api/health',timeout=4)"

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
