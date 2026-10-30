# SupaVic DLNA: music-only DLNA/UPnP server.
# Ready-made image (amd64 + arm64): ghcr.io/nomuraki/supavic-dlna, published by .github/workflows/docker.yml
# Build:  docker build -t supavic-dlna .
# Run:    docker compose up -d   (see docker-compose.yml; host networking is required for DLNA discovery)
# Based on the Docker setup contributed by @apwiggins (#1).

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    SUPAVIC_HOME=/data \
    SUPAVIC_MUSICA=/music

WORKDIR /app

# Dependencies first, so code changes don't reinstall them (pystray is Windows-only and is skipped)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py launcher.py dashboard.py i18n.py ./

# /data: config.json, logs, tag cache and covers (keep it on a volume so it survives rebuilds)
# /music: your library, mounted read-only (the server never writes to it)
VOLUME ["/data"]
EXPOSE 8200/tcp 8202/tcp 1900/udp

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD python -c "import json,urllib.request,os; p=json.load(open(os.environ['SUPAVIC_HOME']+'/config.json'))['servidor']['puerto']; urllib.request.urlopen(f'http://127.0.0.1:{p}/api/estado', timeout=4)" || exit 1

# --sin-bandeja: no tray icon (headless); --inicio: don't try to open a browser
CMD ["python", "launcher.py", "--sin-bandeja", "--inicio"]
