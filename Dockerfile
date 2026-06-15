FROM python:3.11-slim

WORKDIR /app

# Install build deps + PostgreSQL client library for asyncpg + Playwright deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libdbus-1-3 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 libatspi2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium for Playwright
RUN python3 -m playwright install chromium --with-deps

# Copy app code
COPY . .

# Create data directory for SQLite (Railway persistent volume)
RUN mkdir -p /data

# Health check — use PORT env var (Railway provides this)
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD python3 -c "import os,urllib.request; urllib.request.urlopen(f'http://localhost:{os.environ.get(\"PORT\",8000)}/health')"

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]