# syntax=docker/dockerfile:1
# Neolink provides its binary and the required GStreamer runtime.
ARG NEOLINK_IMAGE=ghcr.io/mhhoeper/neolink:dev
FROM ${NEOLINK_IMAGE}
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv ffmpeg tini \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN python3 -m venv /opt/web-venv \
    && /opt/web-venv/bin/pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY LICENSE /app/LICENSE
ARG APP_REVISION=unknown
ARG APP_SOURCE=https://github.com/mhhoeper/neolink-sample-app
ARG APP_SOURCE_URL=https://github.com/mhhoeper/neolink-sample-app
LABEL org.opencontainers.image.title="Neolink Studio" \
      org.opencontainers.image.description="Camera events and on-demand live view powered by Neolink" \
      org.opencontainers.image.source="${APP_SOURCE}" \
      org.opencontainers.image.url="${APP_SOURCE_URL}" \
      org.opencontainers.image.revision="${APP_REVISION}" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"
RUN mkdir -p /data /config && chown -R 10001:10001 /app /data
ENV PATH="/opt/web-venv/bin:${PATH}" \
    NEOLINK_CONFIG=/config/neolink.toml APP_DATA=/data \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
