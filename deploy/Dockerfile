# syntax=docker/dockerfile:1
# Build context is the repository root (the package is self-contained).
# IMPORTANT: the container must run with network_mode: host (see docker-compose.yml).
# In bridge mode the LanSearch broadcast will not reach the camera and UDP :32108
# will not be reachable from outside.
# The HTTP bind comes from config (0.0.0.0 by default). RTSP stays on loopback
# until rtsp.bind and an RTSP password are configured; that avoids starting a
# non-loopback RTSP server without authentication.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY . /app

RUN useradd --system --home /app --shell /usr/sbin/nologin lookcam \
    && mkdir -p /app/config /app/lookcam_data \
    && chown -R lookcam:lookcam /app

USER lookcam
EXPOSE 8080/tcp 8554/tcp 32108/udp

CMD ["python", "-m", "service", "--config", "/app/config/lookcam_service.json", "--cache-dir", "/app/lookcam_data", "--log-level", "INFO"]
