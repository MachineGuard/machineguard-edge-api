FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    EDGE_DATABASE_PATH=/data/edge.db \
    EDGE_BIND_HOST=0.0.0.0 \
    EDGE_PORT=5000

RUN groupadd --system --gid 10001 machineguard \
    && useradd --system --uid 10001 --gid 10001 --home /nonexistent --shell /usr/sbin/nologin machineguard \
    && mkdir /data \
    && chown 10001:10001 /data

WORKDIR /app

COPY requirements.txt ./
RUN pip install --require-hashes -r requirements.txt

COPY edge_api ./edge_api
COPY thingspeak_child ./thingspeak_child

USER 10001:10001
EXPOSE 5000

# /health is administrative: it needs loopback (this check) and the admin key from the container environment.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os, sys, urllib.request; r = urllib.request.Request('http://127.0.0.1:5000/api/v1/edge/health', headers={'X-Edge-Admin-Key': os.environ.get('EDGE_ADMIN_KEY', '')}); sys.exit(0 if urllib.request.urlopen(r, timeout=3).status == 200 else 1)"]

CMD ["python", "-m", "edge_api.server"]
