#!/usr/bin/env bash
#
# Deploy Road Cleaner to Google Cloud.
#
#   ./deploy/deploy.sh PROJECT_ID [REGION]
#
# Creates: Firestore, a GCS bucket with a 7-day lifecycle, Pub/Sub topics with a
# dead-letter queue, secrets, one Cloud Run service and two Cloud Run jobs on a
# schedule.
#
# Safe to re-run: every step is idempotent, and anything that already exists is
# left alone rather than recreated.
#
# DRY_RUN stays true. Nothing here can send a report to a real agency.

set -euo pipefail

PROJECT="${1:-}"
REGION="${2:-us-central1}"
BUCKET="${PROJECT}-road-cleaner-frames"
SERVICE_ACCOUNT="road-cleaner@${PROJECT}.iam.gserviceaccount.com"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/road-cleaner/app:latest"
FRAME_RETENTION_DAYS=7

if [[ -z "${PROJECT}" ]]; then
  echo "usage: $0 PROJECT_ID [REGION]" >&2
  exit 1
fi

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }

gcloud config set project "${PROJECT}" >/dev/null

say "Enabling APIs"
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  firestore.googleapis.com \
  pubsub.googleapis.com \
  cloudscheduler.googleapis.com \
  secretmanager.googleapis.com \
  aiplatform.googleapis.com \
  storage.googleapis.com

say "Service account"
gcloud iam service-accounts create road-cleaner \
  --display-name="Road Cleaner agent" 2>/dev/null || echo "  already exists"

for role in \
  roles/datastore.user \
  roles/storage.objectAdmin \
  roles/pubsub.editor \
  roles/aiplatform.user \
  roles/secretmanager.secretAccessor \
  roles/logging.logWriter
do
  gcloud projects add-iam-policy-binding "${PROJECT}" \
    --member="serviceAccount:${SERVICE_ACCOUNT}" \
    --role="${role}" --condition=None >/dev/null
done
echo "  roles bound"

say "Firestore"
gcloud firestore databases create --location="${REGION}" 2>/dev/null \
  || echo "  already exists"

say "Frame bucket (${FRAME_RETENTION_DAYS}-day retention)"
gcloud storage buckets create "gs://${BUCKET}" --location="${REGION}" 2>/dev/null \
  || echo "  already exists"
# Retention is a bucket policy, not something our code has to remember to do.
# Evidence frames of public roads should not accumulate forever.
cat > /tmp/rc-lifecycle.json <<EOF
{"rule": [{"action": {"type": "Delete"},
           "condition": {"age": ${FRAME_RETENTION_DAYS}}}]}
EOF
gcloud storage buckets update "gs://${BUCKET}" --lifecycle-file=/tmp/rc-lifecycle.json

say "Pub/Sub topics and dead-letter queue"
for topic in frame-captured hazard-confirmed road-cleaner-dlq; do
  gcloud pubsub topics create "${topic}" 2>/dev/null || echo "  ${topic} already exists"
done

say "Secrets"
for secret in ga-511-key fl-511-key nc-511-key; do
  gcloud secrets create "${secret}" --replication-policy=automatic 2>/dev/null \
    || echo "  ${secret} already exists"
done
echo "  add values with:  echo -n 'KEY' | gcloud secrets versions add ga-511-key --data-file=-"

say "Building image"
gcloud artifacts repositories create road-cleaner \
  --repository-format=docker --location="${REGION}" 2>/dev/null || true
gcloud builds submit --tag "${IMAGE}" --file deploy/Dockerfile .

COMMON_ENV="ROAD_CLEANER_MODE=cloud,DRY_RUN=true"
COMMON_ENV="${COMMON_ENV},GOOGLE_CLOUD_PROJECT=${PROJECT}"
COMMON_ENV="${COMMON_ENV},GOOGLE_CLOUD_LOCATION=${REGION}"
COMMON_ENV="${COMMON_ENV},GCS_BUCKET=${BUCKET}"

say "Dashboard service"
gcloud run deploy road-cleaner-dashboard \
  --image="${IMAGE}" \
  --region="${REGION}" \
  --service-account="${SERVICE_ACCOUNT}" \
  --set-env-vars="${COMMON_ENV}" \
  --allow-unauthenticated \
  --min-instances=0 \
  --max-instances=3 \
  --memory=1Gi

say "Watcher job (polls cameras)"
gcloud run jobs deploy road-cleaner-watcher \
  --image="${IMAGE}" \
  --region="${REGION}" \
  --service-account="${SERVICE_ACCOUNT}" \
  --set-env-vars="${COMMON_ENV}" \
  --set-secrets="GA_511_API_KEY=ga-511-key:latest,FL_511_API_KEY=fl-511-key:latest,NC_511_API_KEY=nc-511-key:latest" \
  --command="road-cleaner" --args="run,--once" \
  --max-retries=1 --task-timeout=10m --memory=1Gi

say "Auditor job (re-checks open cases)"
gcloud run jobs deploy road-cleaner-auditor \
  --image="${IMAGE}" \
  --region="${REGION}" \
  --service-account="${SERVICE_ACCOUNT}" \
  --set-env-vars="${COMMON_ENV}" \
  --set-secrets="GA_511_API_KEY=ga-511-key:latest,FL_511_API_KEY=fl-511-key:latest,NC_511_API_KEY=nc-511-key:latest" \
  --command="road-cleaner" --args="audit" \
  --max-retries=1 --task-timeout=15m --memory=1Gi

say "Schedules"
gcloud scheduler jobs create http road-cleaner-watch \
  --location="${REGION}" \
  --schedule="*/5 * * * *" \
  --uri="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT}/jobs/road-cleaner-watcher:run" \
  --http-method=POST \
  --oauth-service-account-email="${SERVICE_ACCOUNT}" 2>/dev/null || echo "  already scheduled"

gcloud scheduler jobs create http road-cleaner-audit \
  --location="${REGION}" \
  --schedule="0 * * * *" \
  --uri="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT}/jobs/road-cleaner-auditor:run" \
  --http-method=POST \
  --oauth-service-account-email="${SERVICE_ACCOUNT}" 2>/dev/null || echo "  already scheduled"

URL=$(gcloud run services describe road-cleaner-dashboard \
        --region="${REGION}" --format='value(status.url)')

cat <<EOF

  Deployed.

  Dashboard   ${URL}
  Watcher     every 5 minutes
  Auditor     hourly
  Frames      gs://${BUCKET} (deleted after ${FRAME_RETENTION_DAYS} days)

  DRY_RUN is ON. No agency will be contacted.

  Next:
    1. Add your 511 developer keys to Secret Manager (command printed above).
    2. Seed the camera registry:
         gcloud run jobs execute road-cleaner-watcher --region=${REGION}
    3. Watch it work:
         gcloud run services logs tail road-cleaner-dashboard --region=${REGION}

  To tear it all down again:
    ./deploy/teardown.sh ${PROJECT} ${REGION}

EOF
