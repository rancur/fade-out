# ==============================================================================
# fade-out — Multi-stage Docker build
# Stage 1: Build frontend (Vite/React)
# Stage 2: Python runtime with ffmpeg, Chromium, Playwright
# ==============================================================================

# ------------------------------------------------------------------------------
# Stage 1: Frontend build
# ------------------------------------------------------------------------------
FROM node:20-alpine AS frontend-build

WORKDIR /app/frontend

COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

# ------------------------------------------------------------------------------
# Stage 2: Runtime
# ------------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# Prevent Python from writing .pyc files and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    chromium \
    chromium-driver \
    fonts-liberation \
    fonts-noto \
    fonts-noto-color-emoji \
    fonts-dejavu-core \
    fonts-freefont-ttf \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set Chromium env vars for Playwright
ENV CHROME_BIN=/usr/bin/chromium \
    CHROMEDRIVER_PATH=/usr/bin/chromedriver \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=0

WORKDIR /app/backend

# Install Python dependencies
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright chromium browser
RUN playwright install chromium --with-deps

# Copy backend source
COPY backend/ ./

# Bake the brand pixel font (Press Start 2P, OFL — bundled in the repo) into
# the system font path the app's PIXEL_FONT_PATH default points at.
RUN mkdir -p /usr/share/fonts/truetype/pressstart2p \
    && cp assets/fonts/PressStart2P-Regular.ttf /usr/share/fonts/truetype/pressstart2p/ \
    && cp assets/fonts/OFL.txt /usr/share/fonts/truetype/pressstart2p/

# Copy frontend build from stage 1
COPY --from=frontend-build /app/frontend/dist /app/frontend/dist

# Build stamp. Baked LAST so it does not bust the layer cache, and exposed on
# /api/health so a running container can be matched against the commit it was
# built from. A version string alone has already proved unreliable: the
# container running in production on 2026-08-14 was built 07-21 and nothing
# said so.
ARG BUILD_COMMIT=unknown
ARG BUILD_TIME=unknown
ARG BUILD_SOURCE=local
ENV BUILD_COMMIT=$BUILD_COMMIT \
    BUILD_TIME=$BUILD_TIME \
    BUILD_SOURCE=$BUILD_SOURCE

# Create persistent data, watch, and output directories
RUN mkdir -p /data /watch/audio /watch/video /watch/djctl-cue \
    /output/thumbnails /output/cover-art

# Volume mount points
VOLUME ["/data", "/watch", "/output"]

EXPOSE 8000

# Container liveness only. /api/health asserts real function (credentials,
# deployment freshness) and returns 503 when the service cannot publish —
# which is a paging condition, not a reason for Docker to cycle the process.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8000/api/health/live || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
