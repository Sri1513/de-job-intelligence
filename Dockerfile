FROM python:3.12-slim

# Prevent Python bytecode generation and configure Chromium paths
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    CHROME_PATH=/usr/bin/chromium

WORKDIR /app

# 1. Install build tools, native ARM64 Chromium, and all required GUI/X11 rendering libraries
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
    git \
    chromium \
    chromium-sandbox \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libcups2 \
    libdrm2 \
    libgbm1 \
    libnss3 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    && rm -rf /var/lib/apt/lists/*

# 2. Symlink chromium to google-chrome so browser-use's watchdog picks it up on step 1
RUN ln -s /usr/bin/chromium /usr/bin/google-chrome

# 3. Install project dependencies
COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 4. Initialize Playwright dependencies for Chromium
RUN playwright install chromium

# 5. Copy project source
COPY . /app

# 6. Ensure runtime artifact directories exist
RUN mkdir -p /app/data/screenshots /app/config/resumes /app/config/auth_states

EXPOSE 8000 5001

CMD ["uvicorn", "src.protocols.app:app", "--host", "0.0.0.0", "--port", "8000"]