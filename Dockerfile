# One image, three entry points.
#
# The dashboard, the Watcher job and the Auditor job all run the same code and
# differ only in the command Cloud Run invokes -- so building one image keeps
# them from ever drifting apart, and there is only one thing to push.
#
# This lives at the repo root rather than under deploy/ because both
# `gcloud run deploy --source .` and `gcloud builds submit --tag` look for
# ./Dockerfile and neither accepts a path to one. The previous deploy script
# passed `--file deploy/Dockerfile`, a flag `gcloud builds submit` does not
# have, so it aborted before creating anything.

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

# The demo slice of data/: the case database, the 26 evidence frames cases
# actually reference, and the generated clips. Assembled by deploy/bundle.py --
# the real data/ is 1.3 GB and must never end up in an image.
COPY deploy/_bundle/ ./data/

# Cloud Run sends SIGTERM and expects a prompt, clean exit.
STOPSIGNAL SIGTERM

# Never run as root. data/ is chowned because SQLite needs to write its WAL
# alongside the database file, even for a read-mostly dashboard.
RUN useradd --create-home --uid 1000 roadcleaner \
    && mkdir -p /app/data \
    && chown -R roadcleaner:roadcleaner /app
USER roadcleaner

ENV ROAD_CLEANER_MODE=local \
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
