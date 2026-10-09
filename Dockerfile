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
# NOTE: no COPY of .env here on purpose - secrets must never be baked into
# an image layer. Provide them at runtime via docker-compose `env_file` or
# `-e` flags instead.
COPY replydesk/ ./replydesk/
COPY products.example.yaml ./products.yaml
COPY .env.example ./.env.example

# Default to dry_run; mount your own .env + products.yaml to override.
# DB_PATH matches .env.example so a bind-mounted ./data volume is picked up
# automatically; the dir is pre-created so a non-root user can write it.
ENV MODE=dry_run \
    DB_PATH=/data/replydesk.db \
    PRODUCTS_FILE=/app/products.yaml

RUN mkdir -p /data && \
    groupadd --system --gid 1001 replydesk && \
    useradd --system --uid 1001 --gid replydesk --home-dir /app replydesk && \
    chown -R replydesk:replydesk /data /app
USER replydesk

# The SQLite DB and any logs go on this volume.
VOLUME ["/data"]

# Health check: cheap - just verify the package imports.
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
    CMD python -c "import replydesk; print('ok')" || exit 1

CMD ["python", "-m", "replydesk", "run"]