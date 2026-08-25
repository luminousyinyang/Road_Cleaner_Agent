#!/usr/bin/env bash
#
# Deploy Road Cleaner to Google Cloud.
#
#   ./deploy/deploy.sh PROJECT_ID [REGION]
#
# Deploys the dashboard to Cloud Run and nothing else. That is deliberate: it is
# the shortest path to a working, demonstrable deployment, and every extra piece
# is another thing that can fail on camera.
#
#   --with-firestore   also create Firestore + its composite indexes and run the
#                      service against it instead of the bundled SQLite
#   --with-fleet       also deploy the Watcher/Auditor jobs and their schedules
#
# Safe to re-run: every step is idempotent.
#
# DRY_RUN stays true. Nothing here can send a report to a real agency.

set -euo pipefail

PROJECT=""
REGION="us-central1"
WITH_FIRESTORE=0
WITH_FLEET=0

for arg in "$@"; do
  case "${arg}" in
    --with-firestore) WITH_FIRESTORE=1 ;;
    --with-fleet)     WITH_FLEET=1 ;;
    -*) echo "unknown flag: ${arg}" >&2; exit 1 ;;
    *) if [[ -z "${PROJECT}" ]]; then PROJECT="${arg}"; else REGION="${arg}"; fi ;;
  esac
done

if [[ -z "${PROJECT}" ]]; then
  echo "usage: $0 PROJECT_ID [REGION] [--with-firestore] [--with-fleet]" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_ACCOUNT="road-cleaner@${PROJECT}.iam.gserviceaccount.com"
BUCKET="${PROJECT}-road-cleaner-frames"
FRAME_RETENTION_DAYS=7

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }

cd "${REPO_ROOT}"
gcloud config set project "${PROJECT}" >/dev/null

say "Assembling the demo bundle"
# Without this the image has no cases to show. bundle.py copies the case
# database, the 26 referenced evidence frames and the generated clips -- ~99 MB,
# versus 1.3 GB for the real data directory.
python deploy/bundle.py

say "Enabling APIs"
APIS="run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com aiplatform.googleapis.com"
[[ ${WITH_FIRESTORE} -eq 1 ]] && APIS="${APIS} firestore.googleapis.com"
[[ ${WITH_FLEET} -eq 1 ]] && APIS="${APIS} pubsub.googleapis.com cloudscheduler.googleapis.com secretmanager.googleapis.com storage.googleapis.com"
# shellcheck disable=SC2086
gcloud services enable ${APIS}

say "Service account"
gcloud iam service-accounts create road-cleaner \
  --display-name="Road Cleaner agent" 2>/dev/null || echo "  already exists"

ROLES="roles/aiplatform.user roles/logging.logWriter"
[[ ${WITH_FIRESTORE} -eq 1 ]] && ROLES="${ROLES} roles/datastore.user"
if [[ ${WITH_FLEET} -eq 1 ]]; then
  # run.invoker is what Cloud Scheduler needs to POST jobs:run. Without it both
  # schedules are created successfully and then 403 on every single fire.
  ROLES="${ROLES} roles/storage.objectAdmin roles/pubsub.editor roles/secretmanager.secretAccessor roles/run.invoker"
fi
for role in ${ROLES}; do
  gcloud projects add-iam-policy-binding "${PROJECT}" \
    --member="serviceAccount:${SERVICE_ACCOUNT}" \
    --role="${role}" --condition=None >/dev/null
done
echo "  roles bound: ${ROLES}"

ENV_VARS="DRY_RUN=true,GOOGLE_CLOUD_PROJECT=${PROJECT}"
# Gemini is served from `global`; Veo and Lyria only from a pinned region.
ENV_VARS="${ENV_VARS},GOOGLE_CLOUD_LOCATION=global,VERTEX_MEDIA_LOCATION=${REGION}"
# The whole point of deploying is to show the real models working.
ENV_VARS="${ENV_VARS},VISION_PROVIDER=gemini,USE_ADK=true,MEDIA_PROVIDER=vertex"

# --- the live demonstration send -------------------------------------------
#
# These live in `.env`, which is excluded from the image on purpose -- secrets
# do not belong in a layer. The consequence was quiet and confusing: the
# deployed site had no recipient, no allowlist and no mail server, so
# `/api/demo/send` reported itself unconfigured, the send button stayed hidden,
# and the case page offered the ordinary manual handover instead. It looked like
# the feature had not been deployed. It had; it just had nothing to send with.
#
# So they are read from the environment (or `.env`) at deploy time and passed
# through -- everything except the password, which goes to Secret Manager. A
# password in `--set-env-vars` is stored in plain text on the service config and
# readable by anyone with Viewer on the project.
SMTP_PASSWORD_VALUE=""
if [[ -f .env ]]; then
  # Only the keys this needs, and only if not already set in the environment.
  while IFS='=' read -r key value; do
    case "${key}" in
      SMTP_HOST|SMTP_PORT|SMTP_USER|SMTP_PASSWORD|FILING_FROM_ADDRESS|DEMO_SEND_TO|LIVE_FILING_ALLOWLIST)
        [[ -n "${!key:-}" ]] || printf -v "${key}" '%s' "${value}"
        ;;
    esac
  done < <(grep -E '^(SMTP_|FILING_FROM_ADDRESS|DEMO_SEND_TO|LIVE_FILING_ALLOWLIST)' .env || true)
fi

if [[ -n "${DEMO_SEND_TO:-}" && -n "${LIVE_FILING_ALLOWLIST:-}" && -n "${SMTP_HOST:-}" ]]; then
  say "Live demonstration send"
  ENV_VARS="${ENV_VARS},DEMO_SEND_TO=${DEMO_SEND_TO},LIVE_FILING_ALLOWLIST=${LIVE_FILING_ALLOWLIST}"
  ENV_VARS="${ENV_VARS},SMTP_HOST=${SMTP_HOST},SMTP_PORT=${SMTP_PORT:-587}"
  ENV_VARS="${ENV_VARS},SMTP_USER=${SMTP_USER:-},FILING_FROM_ADDRESS=${FILING_FROM_ADDRESS:-${SMTP_USER:-}}"

  if [[ -n "${SMTP_PASSWORD:-}" ]]; then
    # Only enabled by --with-fleet otherwise, and this path needs it whether or
    # not the jobs are being deployed.
    gcloud services enable secretmanager.googleapis.com
    gcloud secrets create road-cleaner-smtp-password --replication-policy=automatic \
      2>/dev/null || true
    printf '%s' "${SMTP_PASSWORD}" \
      | gcloud secrets versions add road-cleaner-smtp-password --data-file=- >/dev/null
    gcloud secrets add-iam-policy-binding road-cleaner-smtp-password \
      --member="serviceAccount:${SERVICE_ACCOUNT}" \
      --role=roles/secretmanager.secretAccessor >/dev/null
    SMTP_SECRET="--set-secrets=SMTP_PASSWORD=road-cleaner-smtp-password:latest"
    echo "  recipient ${DEMO_SEND_TO} · password in Secret Manager"
  else
    SMTP_SECRET=""
    echo "  WARNING: no SMTP_PASSWORD found, so the send will fail to authenticate"
  fi
else
  SMTP_SECRET=""
  echo "  demonstration send not configured — set DEMO_SEND_TO, LIVE_FILING_ALLOWLIST"
  echo "  and SMTP_* in .env to deploy it. The site works without it; the send"
  echo "  button simply stays hidden."
fi

if [[ ${WITH_FIRESTORE} -eq 1 ]]; then
  say "Firestore"
  gcloud firestore databases create --location="${REGION}" 2>/dev/null \
    || echo "  already exists"
  # Every composite index the repository's queries need. Without these the
  # dashboard throws FAILED_PRECONDITION on its first list_cases call.
  gcloud firestore indexes composite create --collection-group=cases \
    --field-config=field-path=state,order=ascending \
    --field-config=field-path=opened_at,order=descending 2>/dev/null || true
  gcloud firestore indexes composite create --collection-group=cases \
    --field-config=field-path=kind,order=ascending \
    --field-config=field-path=opened_at,order=descending 2>/dev/null || true
  echo "  indexes requested (they build asynchronously — check the console)"
  ENV_VARS="${ENV_VARS},ROAD_CLEANER_MODE=cloud,REPOSITORY=firestore,BLOB_STORE=local,EVENT_BUS=memory"
else
  # SQLite and the frames ship inside the image. Read-mostly and perfectly
  # adequate for a dashboard; writes do not survive a cold start, which is fine
  # because the deployed instance is a demo surface, not the system of record.
  ENV_VARS="${ENV_VARS},ROAD_CLEANER_MODE=local"
fi

say "Building and deploying the dashboard"
# `run deploy --source .` runs Cloud Build for us and reads ./Dockerfile.
# The previous script called `gcloud builds submit --file deploy/Dockerfile`,
# and `builds submit` has no --file flag, so it never got this far.
#
# The instance flags below are load-bearing, not tuning. Drills, inspections,
# demonstration sends and renders all keep their job registries in
# `app.state.*` -- in the process, not in a database -- and the browser polls
# `/api/inspect/{id}` for progress. That combination has exactly one safe shape
# on Cloud Run:
#
# * `--max-instances=1`, because a poll routed to a second instance asks about a
#   job that instance has never heard of and gets a 404. That is not
#   hypothetical: a deployed run returned 200 five times and then 404, while the
#   Gemini calls carried on in the logs -- the work was fine, the poll had simply
#   landed somewhere else, and the page gave up on a run that was still going.
# * `--min-instances=1`, because a cold start takes the same registries with it.
# * `--no-cpu-throttling`, because the work runs in a task created *after* the
#   202 is returned, and CPU outside request handling is throttled to almost
#   nothing by default.
#
# Raising max-instances means moving job state out of the process first.
gcloud run deploy road-cleaner-dashboard \
  --source . \
  --region="${REGION}" \
  --service-account="${SERVICE_ACCOUNT}" \
  --set-env-vars="${ENV_VARS}" \
  ${SMTP_SECRET} \
  --allow-unauthenticated \
  --min-instances=1 \
  --max-instances=1 \
  --no-cpu-throttling \
  --session-affinity \
  --memory=2Gi \
  --cpu=2 \
  --timeout=600

URL=$(gcloud run services describe road-cleaner-dashboard \
        --region="${REGION}" --format='value(status.url)')

# A second pass, because a Cloud Run URL carries a generated hash and cannot be
# predicted before the first deploy. It is what lets a report link the marked
# still; unset, the report just omits the link rather than putting a
# site-relative path in somebody's inbox.
say "Telling the service its own address"
gcloud run services update road-cleaner-dashboard \
  --region="${REGION}" \
  --update-env-vars="PUBLIC_BASE_URL=${URL}" \
  --quiet

if [[ ${WITH_FLEET} -eq 1 ]]; then
  say "Frame bucket (${FRAME_RETENTION_DAYS}-day retention)"
  gcloud storage buckets create "gs://${BUCKET}" --location="${REGION}" 2>/dev/null \
    || echo "  already exists"
  cat > /tmp/rc-lifecycle.json <<EOF
{"rule": [{"action": {"type": "Delete"},
           "condition": {"age": ${FRAME_RETENTION_DAYS}}}]}
EOF
  gcloud storage buckets update "gs://${BUCKET}" --lifecycle-file=/tmp/rc-lifecycle.json

  say "Pub/Sub topics"
  for topic in frame-captured hazard-confirmed road-cleaner-dlq; do
    gcloud pubsub topics create "${topic}" 2>/dev/null || echo "  ${topic} exists"
  done
  echo "  NOTE: no subscriptions are created. Nothing consumes these topics yet"
  echo "        (there is no Pub/Sub push route), so in cloud mode the Analyst"
  echo "        would never see a published frame. See docs/architecture.md."

  IMAGE=$(gcloud run services describe road-cleaner-dashboard \
            --region="${REGION}" --format='value(spec.template.spec.containers[0].image)')
  for job in "watcher:run,--once:10m" "auditor:audit:15m"; do
    name="${job%%:*}"; rest="${job#*:}"; args="${rest%%:*}"; timeout="${rest##*:}"
    say "${name} job"
    gcloud run jobs deploy "road-cleaner-${name}" \
      --image="${IMAGE}" \
      --region="${REGION}" \
      --service-account="${SERVICE_ACCOUNT}" \
      --set-env-vars="${ENV_VARS}" \
      --command="road-cleaner" --args="${args}" \
      --max-retries=1 --task-timeout="${timeout}" --memory=1Gi
  done
fi

cat <<EOF

  Deployed.

  Dashboard   ${URL}
  Mode        $([[ ${WITH_FIRESTORE} -eq 1 ]] && echo "cloud (Firestore)" || echo "local (SQLite baked into the image)")
  Models      Gemini + ADK on, Veo enabled

  DRY_RUN is ON. No agency will be contacted.

  Check it:
    curl -s ${URL}/api/healthz
    open ${URL}

  Proof for the demo video:
    gcloud run services list --region=${REGION}
    gcloud run services logs tail road-cleaner-dashboard --region=${REGION}

  Turn it off again when you are done filming:
    ./deploy/teardown.sh ${PROJECT} ${REGION}

EOF
