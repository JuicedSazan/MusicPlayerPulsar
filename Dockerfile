# Dockerfile for hosts with a real 1 GB+ container (not needed on Render Free).
#
# The VK login window needs a headless Chromium with its system libraries; the
# image below installs Playwright's Chromium and the worker launches the app.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PORT=3000

# Chromium system libraries (Playwright's documented package set).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl fonts-liberation \
        libasound2 libatk-bridge2.0-0 libatk1.0-0 libatspi2.0-0 \
        libcairo2 libcups2 libdbus-1-3 libdrm2 libgbm1 libglib2.0-0 \
        libnspr4 libnss3 libpango-1.0-0 libx11-6 libxcb1 libxcomposite1 \
        libxdamage1 libxext6 libxfixes3 libxkbcommon0 libxrandr2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install chromium

COPY . .

EXPOSE 3000
CMD ["gunicorn", "server:app", "--workers", "1", "--threads", "8", "--timeout", "180", "--bind", "0.0.0.0:3000"]
