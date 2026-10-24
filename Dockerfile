# Image de production (conteneur Docker sur le serveur Proxmox).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml ./
COPY mcs ./mcs
RUN pip install . && useradd --create-home --uid 1000 mcs && mkdir -p /data /app/logs \
    && chown -R mcs /data /app/logs
USER mcs

# Base sur un volume local (jamais un partage réseau) : WAL actif.
ENV DB_PATH=/data/mcs.sqlite \
    SQLITE_JOURNAL_MODE=WAL \
    LOG_DIR=/app/logs

CMD ["python", "-m", "mcs.collector", "live"]
