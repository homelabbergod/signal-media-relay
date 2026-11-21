# Minor version pinned; every rebuild (docker compose build --pull) brings in security patches.
FROM python:3.14-slim-trixie

# Must match __version__ in relay.py
ARG VERSION=0.9.2
LABEL org.opencontainers.image.title="signal-media-relay" \
      org.opencontainers.image.description="Announces new movies/episodes from Sonarr, Radarr or Jellyfin in a Signal group; !request from the chat" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.licenses="GPL-3.0" \
      org.opencontainers.image.source="https://github.com/homelabbergod/signal-media-relay" \
      org.opencontainers.image.url="https://github.com/homelabbergod/signal-media-relay" \
      org.opencontainers.image.documentation="https://github.com/homelabbergod/signal-media-relay#readme"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# - install Debian security updates that are still missing in the base image
# - remove pip/ensurepip: not needed at runtime (relay.py only uses the stdlib),
#   but it ships its own packages (urllib3, msgpack, setuptools, ...) with their own CVEs
# - dedicated unprivileged user; the code belongs to root and is read-only for the service
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/* \
 && python -m pip uninstall -y pip \
 && rm -rf /usr/local/lib/python3.*/ensurepip /usr/local/bin/pip* \
 && useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin relay
WORKDIR /app
COPY --chmod=0444 relay.py .

USER 10001:10001
EXPOSE 8090
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
  CMD ["python", "-c", "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('LISTEN_PORT','8090'),timeout=3)"]

CMD ["python", "relay.py"]
