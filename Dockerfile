# Minor-Version fixiert; bei jedem Rebuild (docker compose build --pull) kommen Sicherheits-Patches.
FROM python:3.14-slim-trixie

# Muss zu __version__ in relay.py passen (beim Bauen: --build-arg VERSION=... ist optional)
ARG VERSION=0.8.0
LABEL org.opencontainers.image.title="signal-media-relay" \
      org.opencontainers.image.description="Announces new movies/episodes from Sonarr, Radarr or Jellyfin in a Signal group; !request from the chat" \
      org.opencontainers.image.version="${VERSION}"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# - Debian-Sicherheitsupdates einspielen, die im Basis-Image noch fehlen
# - pip/ensurepip entfernen: wird zur Laufzeit nicht gebraucht (relay.py nutzt nur die Stdlib),
#   bringt aber eigene Pakete (urllib3, msgpack, setuptools, ...) mit eigenen CVEs mit
# - eigener unprivilegierter Benutzer; Code gehoert root und ist fuer den Dienst nur lesbar
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
