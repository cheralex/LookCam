# syntax=docker/dockerfile:1
# Build context is the lookcam_deepsik directory (the package is self-contained).
# IMPORTANT: the container must run with network_mode: host (see docker-compose.yml).
# In bridge mode the LanSearch broadcast will not reach the camera and UDP :32108
# will not be reachable from outside.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY . /app/lookcam_deepsik

RUN useradd --system --home /app --shell /usr/sbin/nologin lookcam \
    && mkdir -p /app/lookcam_data /app/lookcam_deepsik/config \
    && chown -R lookcam:lookcam /app

USER lookcam
EXPOSE 8080/tcp 8554/tcp 32108/udp

CMD ["python", "-m", "lookcam_deepsik.service", "--config", "/app/lookcam_deepsik/config/lookcam_service.json", "--cache-dir", "/app/lookcam_data", "--bind", "0.0.0.0", "--log-level", "INFO"]
