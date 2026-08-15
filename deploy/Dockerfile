# One image, three entry points.
#
# The dashboard, the Watcher job and the Auditor job all run the same code and
# differ only in the command Cloud Run invokes -- so building one image keeps
# them from ever drifting apart, and there is only one thing to push.

FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, so a source change doesn't reinstall the world.
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir -e ".[cloud]"

# Seed data: the camera registry, jurisdiction rules and scenario timeline.
# agencies.yaml in particular is not optional -- it is how the Dispatcher knows
# who owns which road.
COPY seeds/ ./seeds/

# Cloud Run sends SIGTERM and expects a prompt, clean exit.
STOPSIGNAL SIGTERM

# Never run as root.
RUN useradd --create-home --uid 1000 roadcleaner \
    && mkdir -p /app/data \
    && chown -R roadcleaner:roadcleaner /app
USER roadcleaner

ENV ROAD_CLEANER_MODE=cloud \
    DRY_RUN=true \
    WEB_HOST=0.0.0.0 \
    DATA_DIR=/app/data

# Cloud Run injects $PORT; default for local `docker run`.
ENV PORT=8080
EXPOSE 8080

# Default: the dashboard service. The jobs override this:
#   Watcher  ->  road-cleaner run
#   Auditor  ->  road-cleaner audit
CMD exec uvicorn road_cleaner.web.app:create_app --factory \
    --host 0.0.0.0 --port ${PORT} --workers 1
