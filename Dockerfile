FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Tehran \
    XRAY_LOCATION=/usr/local/bin/xray \
    DATA_DIR=/app/data \
    PORT=8080

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    unzip \
    ca-certificates \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Install Xray-core (latest stable)
RUN XRAY_VERSION=$(curl -s https://api.github.com/repos/XTLS/Xray-core/releases/latest | grep -oP '"tag_name": "\K(.*)(?=")') \
    && curl -L -o /tmp/xray.zip "https://github.com/XTLS/Xray-core/releases/download/${XRAY_VERSION}/Xray-linux-64.zip" \
    && unzip /tmp/xray.zip -d /tmp/xray \
    && mv /tmp/xray/xray /usr/local/bin/xray \
    && chmod +x /usr/local/bin/xray \
    && rm -rf /tmp/xray /tmp/xray.zip \
    && xray version

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY app/ ./app/
COPY data/ ./data/

# Railway Volume: mount to /app/data in Railway dashboard (do not use Dockerfile VOLUME)

EXPOSE 8080

# Railway sets $PORT — fall back to 8080
CMD sh -c "python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"
