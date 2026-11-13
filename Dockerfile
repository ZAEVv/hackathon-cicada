# Smart Caspian Transit: one container, two processes.
# The API listens on the container's loopback only; the only published port is the UI
# (vite preview), which proxies /api to 127.0.0.1:8000. This keeps the API's security model
# intact: default dev tokens are accepted only from loopback clients, and here the proxy is one.

# --- stage 1: frontend dependencies + production bundle ------------------------------
FROM node:22-bookworm-slim AS web
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# --- stage 2: runtime ----------------------------------------------------------------
FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1 \
    DATABASE_PATH=/data/smart_caspian_demo.sqlite3 \
    WEATHER_PROVIDER=open-meteo
# Node is needed only to run `vite preview`.
COPY --from=web /usr/local/bin/node /usr/local/bin/node
WORKDIR /app

# Dependencies first: this layer is cached until requirements.txt changes.
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY backend/ ./backend/
COPY --from=web /app/frontend/ ./frontend/
COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN useradd --system --uid 10001 --home /app app \
    && mkdir -p /data && chown app /data \
    && chmod +x /usr/local/bin/docker-entrypoint.sh
USER app
VOLUME /data
EXPOSE 5173
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"
ENTRYPOINT ["docker-entrypoint.sh"]
