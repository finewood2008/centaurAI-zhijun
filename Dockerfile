# syntax=docker/dockerfile:1

# Build the Vue production assets separately so the runtime image does not need Node.js.
FROM node:20-bookworm-slim AS web-build
WORKDIR /build/frontend/mindos-web
COPY frontend/mindos-web/package.json frontend/mindos-web/package-lock.json ./
RUN npm ci
COPY frontend/mindos-web/ ./
RUN npm run build

FROM python:3.11-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONNOUSERSITE=1 \
    PIP_NO_CACHE_DIR=1 \
    TOKENIZERS_PARALLELISM=false \
    CENTAURAI_DATABASE_DATA_ROOT=/var/lib/mindos

# Runtime libraries cover document parsing, OpenCV/RapidOCR, and optional audio/video handling.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        libgomp1 \
        libgl1 \
        libglib2.0-0 \
        libsm6 \
        libxext6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/mindos/app

COPY backend/requirements-lock.txt /tmp/requirements-lock.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install -r /tmp/requirements-lock.txt

COPY backend/ ./backend/
COPY scripts/ ./scripts/
COPY run.sh ./run.sh
COPY --from=web-build /build/frontend/mindos-web/dist ./frontend/mindos-web/dist

# The application only needs the assets exposed by server.py; source Node modules stay in the build stage.
COPY frontend/assets/ ./frontend/assets/
COPY frontend/mobile/ ./frontend/mobile/
COPY frontend/renderer/lan_import.html ./frontend/renderer/lan_import.html
COPY frontend/package.json ./frontend/package.json

RUN groupadd --system --gid 10001 mindos \
    && useradd --system --uid 10001 --gid mindos --home-dir /nonexistent --shell /usr/sbin/nologin mindos \
    && mkdir -p /var/lib/mindos \
    && chown -R mindos:mindos /opt/mindos/app /var/lib/mindos \
    && chmod 0755 /opt/mindos/app/run.sh

COPY docker/entrypoint.sh /usr/local/bin/mindos-entrypoint
RUN sed -i 's/\r$//' /opt/mindos/app/run.sh /usr/local/bin/mindos-entrypoint \
    && chmod 0755 /opt/mindos/app/run.sh /usr/local/bin/mindos-entrypoint

USER mindos
EXPOSE 8618
ENTRYPOINT ["/usr/local/bin/mindos-entrypoint"]
CMD ["/opt/mindos/app/run.sh"]
