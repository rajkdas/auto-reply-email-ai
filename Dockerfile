FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install Python deps first (better Docker layer caching).
COPY requirements.txt .
RUN pip install -r requirements.txt

# Copy the package, config examples, and tests.
COPY replydesk/ ./replydesk/
COPY products.example.yaml ./products.yaml
COPY .env.example ./.env.example

# Default to dry_run; mount your own .env + products.yaml to override.
ENV MODE=dry_run \
    DB_PATH=/data/replydesk.db

# The SQLite DB and any logs go on this volume.
VOLUME ["/data"]

# Health check: cheap - just verify the package imports.
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
    CMD python -c "import replydesk; print('ok')" || exit 1

CMD ["python", "-m", "replydesk", "run"]
