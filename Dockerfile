FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    CONFIG_PATH=/config/config.yaml \
    STATE_DB=/state/state.db

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps .

# Runs as a normal user; override with PUID/PGID at build time to line up with
# the owner of the Synology share.
ARG PUID=1026
ARG PGID=100
RUN groupadd -g "${PGID}" -o app 2>/dev/null || true \
 && useradd -u "${PUID}" -g "${PGID}" -o -m -s /usr/sbin/nologin app \
 && mkdir -p /state /config /data \
 && chown -R "${PUID}:${PGID}" /state

USER ${PUID}:${PGID}

VOLUME ["/state"]

# The watcher touches the heartbeat file at the end of every scan.
HEALTHCHECK --interval=2m --timeout=10s --start-period=1m --retries=3 \
    CMD python -c "import os,sys,time; p='/state/heartbeat'; sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p) < 900 else 1)"

ENTRYPOINT ["python", "-m", "frameio_export_watcher"]
CMD ["run"]
