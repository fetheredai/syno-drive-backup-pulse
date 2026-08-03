# Synology Drive Backup Pulse
# Runs on the NAS it monitors. Multi-arch: linux/amd64 (Intel/AMD "plus"
# models) and linux/arm64 (Realtek/ARM models).
FROM python:3.12-slim

LABEL org.opencontainers.image.title="Synology Drive Backup Pulse" \
      org.opencontainers.image.description="Per-user Synology Drive Client backup health dashboard" \
      org.opencontainers.image.source="https://github.com/esozo/syno-drive-backup-pulse"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SYNO_WEB_DIR=/app/web \
    SYNO_WEB_PORT=8477 \
    SYNO_OUTPUT=/app/web/data.json \
    SYNO_INTERVAL_HOURS=4 \
    SYNO_HOST=localhost \
    SYNO_PORT=5000 \
    SYNO_HTTPS=false \
    SYNO_VERIFY_SSL=false \
    SYNO_DAYS=90

WORKDIR /app

# requests is the only runtime dependency
RUN pip install --no-cache-dir requests==2.32.3

COPY collector.py app.py ./
COPY web/ ./web/

# Non-root. web/ must stay writable — that's where data.json lands.
RUN useradd --system --uid 10001 --create-home pulse \
    && chown -R pulse:pulse /app/web
USER pulse

EXPOSE 8477

HEALTHCHECK --interval=60s --timeout=5s --start-period=15s --retries=3 \
  CMD python3 -c "import os,urllib.request,sys; \
u='http://127.0.0.1:'+os.environ.get('SYNO_WEB_PORT','8477')+'/healthz'; \
sys.exit(0 if urllib.request.urlopen(u,timeout=4).status==200 else 1)"

CMD ["python3", "app.py"]
