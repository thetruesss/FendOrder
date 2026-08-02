# =============================================================================
#  Ozon B2B Engine v2.0 — runtime image with Camoufox preinstalled (spec §7.2)
# =============================================================================
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Gecko needs a real (if headless) GTK stack; without these Camoufox exits
# immediately with a shared-library error that reads like a crash.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        fonts-liberation \
        libasound2 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libcairo2 \
        libcups2 \
        libdbus-glib-1-2 \
        libdrm2 \
        libgbm1 \
        libgtk-3-0 \
        libnss3 \
        libpango-1.0-0 \
        libx11-xcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
        libxt6 \
        libxtst6 \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

# Run as an unprivileged user. Camoufox caches the browser under the *invoking*
# user's data directory, so the user must exist before `fetch` runs — doing it
# as root would put the binary somewhere this user cannot read.
RUN useradd --create-home --uid 10001 engine
COPY --chown=engine:engine . /app
USER engine

RUN python -m camoufox fetch

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/v1/health || exit 1

CMD ["uvicorn", "ozon_anti_bot_engine.main:app", "--host", "0.0.0.0", "--port", "8000"]
