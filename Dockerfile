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

# The image is built from a snapshot of src/ and ships no .git, so the commit
# has to be injected here; src/version.py reports it in /start and /status.
ARG BUILD_ID=unknown
ENV BUILD_ID=$BUILD_ID

# Copy application code
COPY src/ ./src/
COPY .env.example ./.env.example

# Fail the build when a variable is read but not declared, or when
# .env.example was edited by hand instead of being regenerated from
# src/config/env_schema.py.  Both drift silently at runtime otherwise.
RUN python src/config/env_sync.py --audit \
 && python src/config/env_sync.py --check-example

# Create necessary directories
RUN mkdir -p data logs

# Align the mounted .env with the code before the bot reads it:
# new options are appended with their code defaults, options that no longer
# exist are removed.  Existing values are never touched.  A failure here must
# not stop the bot, hence ';' rather than '&&'.
CMD ["sh", "-c", "python src/config/env_sync.py --apply --quiet; exec python src/main.py"]
