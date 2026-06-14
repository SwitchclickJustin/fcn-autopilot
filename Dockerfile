FROM python:3.11-slim

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libffi-dev curl git \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Browser Use CLI (needed for cloud browser provisioning)
RUN curl -fsSL https://browser-use.com/cli/install.sh | bash

# Copy app code
COPY . .

# Ensure browser-use is in PATH
ENV PATH="/root/.browser-use-env/bin:$PATH"

# Create data directory for SQLite (Railway persistent volume)
RUN mkdir -p /data

# Health check — use PORT env var (Railway provides this)
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD python3 -c "import os,urllib.request; urllib.request.urlopen(f'http://localhost:{os.environ.get(\"PORT\",8000)}/health')"

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]