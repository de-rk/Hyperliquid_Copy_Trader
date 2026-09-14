FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Telegram is optional. Keep it enabled by default for existing deployments
# that already provide TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID.
ARG INSTALL_TELEGRAM=true
COPY requirements-telegram.txt .
RUN if [ "$INSTALL_TELEGRAM" = "true" ]; then \
      pip install --no-cache-dir -r requirements-telegram.txt; \
    fi

# Copy application code
COPY src/ ./src/

# Create necessary directories
RUN mkdir -p data logs

# Run the bot
CMD ["python", "src/main.py"]
