#!/usr/bin/env bash
#
# Tear down everything deploy.sh created.
#
#   ./deploy/teardown.sh PROJECT_ID [REGION]
#
# The hackathon cost guidance is to turn everything off after recording. This is
# that. Firestore data is left alone -- deleting a database is not something a
# script should do behind your back -- but everything that bills by the hour
# goes.

set -euo pipefail

PROJECT="${1:-}"
REGION="${2:-us-central1}"
BUCKET="${PROJECT}-road-cleaner-frames"

if [[ -z "${PROJECT}" ]]; then
  echo "usage: $0 PROJECT_ID [REGION]" >&2
  exit 1
fi

read -rp "Tear down Road Cleaner in ${PROJECT}? [y/N] " confirm
[[ "${confirm}" == "y" ]] || { echo "Nothing done."; exit 0; }

gcloud config set project "${PROJECT}" >/dev/null

echo "==> Schedules"
for job in road-cleaner-watch road-cleaner-audit; do
  gcloud scheduler jobs delete "${job}" --location="${REGION}" --quiet 2>/dev/null || true
done

echo "==> Cloud Run"
gcloud run services delete road-cleaner-dashboard --region="${REGION}" --quiet 2>/dev/null || true
for job in road-cleaner-watcher road-cleaner-auditor; do
  gcloud run jobs delete "${job}" --region="${REGION}" --quiet 2>/dev/null || true
done

echo "==> Pub/Sub"
for topic in frame-captured hazard-confirmed road-cleaner-dlq; do
  gcloud pubsub topics delete "${topic}" --quiet 2>/dev/null || true
done

echo "==> Frames"
gcloud storage rm -r "gs://${BUCKET}" --quiet 2>/dev/null || true

cat <<EOF

  Torn down. Nothing is billing by the hour any more.

  Left in place on purpose:
    - Firestore data (delete it yourself if you want it gone)
    - Secrets (they cost nothing and you'll want them again)
    - The Artifact Registry image

EOF
