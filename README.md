# Road Cleaner

**An autonomous agent fleet that watches 2,000+ Southeast DOT traffic cameras, spots the
hazards official feeds miss, figures out which agency owns the road, files the report, and
keeps watching until it's actually fixed.**

Detection is the easy part. Plenty of things can spot debris on a road. The hard part is
everything after seeing it — working out whose road it is, saying so through the right
channel, and then actually checking whether anybody came. Waze crowdsources sightings and
reports to nobody. DOT cameras see everything and nobody is watching. No system closes the
loop.

Road Cleaner closes the loop. Nobody talks to it. It runs continuously, and its output is
*filed reports with case numbers* and a resolution audit trail.

---

## Quickstart — no credentials, no cloud account, four commands

```bash
make setup     # venv + dependencies + .env
make demo      # watch a simulated week of roads, then open the dashboard
```

Then visit **http://127.0.0.1:8080**.

That works on a clean clone with no API keys of any kind. `make demo` runs the real
four-agent pipeline over a simulated week: it polls simulated cameras that render actual
road-scene JPEGs, runs vision analysis, gates detections, resolves jurisdiction, composes
reports, and re-checks until hazards clear or escalate. A typical run:

```
Camera polls          35,447
  unchanged, skipped   7,043 (20%)
  cameras offline        127
Frames published      28,277
  killed by prefilter 14,314 (51%)
  sent to vision      13,963
Detections             9,065
Reports filed             13
Escalated                  4
Flagged for a human        2
Cleared                    4

4 cleared · 3 escalated · 2 filed · 1 suppressed · 1 watching
```

Every one of those reports is fully composed and written to `data/outbox/`. **Nothing is
sent anywhere.** Read one with `make outbox`.

Other useful commands:

```bash
make doctor    # which adapter is wired to each port, and what's missing to go live
make test      # the whole suite — 225 tests, still no credentials
make outbox    # the reports that would have been sent
make clean     # delete all generated data
```

---

## What actually runs today

Being precise about this, because "it works" is doing a lot of work in most READMEs:

| | Status |
|---|---|
| Four-agent pipeline, seven-stage loop | **Real.** Runs end to end. |
| Confidence gate, SLA, escalation, jurisdiction rules | **Real.** Pure logic, exhaustively tested. |
| Dashboard, case detail, audit trail, evidence frames | **Real.** Server-rendered from the database. |
| Camera feeds | **Simulated.** All three state APIs need a developer key (see below). |
| Vision analysis | **Scripted.** Gemini adapter is written; no key available yet. |
| Report filing | **Dry run.** Composed in full, written to disk, never transmitted. |
| Firestore / GCS / Pub/Sub / Cloud Run | **Written, not exercised.** Deploy scripts included. |

The whole design exists to make that gap an env-var flip rather than a rewrite. Every
external dependency sits behind a port with two implementations.

---

## How it works

Seven stages, per camera, continuously:

```
WATCH → DETECT → CONFIRM → RESOLVE → REPORT → CHECK BACK → PUSH
```

**① Watcher** polls cameras on tiers — busy corridors every 2 minutes, quiet rural cameras
every 10, any camera with an open case every minute. Identical frames are skipped, capped
at ten in a row.

**② Analyst** runs a cheap prefilter (is anything odd here at all?), then full vision
analysis on survivors, then **the confidence gate** — the part that matters:

1. **Floor** — below 0.55 confidence, discard.
2. **Persistence** — one frame is a maybe, not a yes. Requires a second sighting 90s–30min later.
3. **Duplicate** — if the state's own feed already has an active event within 500m, stand down. *Catching what they missed is the entire point.*
4. **Severity × confidence** — critical 0.60, high 0.70, medium 0.80, low 0.88. A person walking on an interstate clears a lower bar than debris, because the cost of being wrong isn't symmetrical.

**③ Dispatcher** resolves jurisdiction with rules first, a model only when they can't
decide, and holds the case rather than guessing if it still can't. Toll authorities own
their own roads; cities own signals on state routes. Then it composes a report and files it
through whatever the agency accepts — Open311, a maintenance form, or structured email.

**④ Auditor** goes back to the same camera and compares against the original evidence
photo. Cleared → close with a before/after pair. Overdue → file again one tier up. Overdue
twice → **stop filing** and flag a human.

That last point is the whole product. Filing a report and walking away is what every
existing tool does, and it's why potholes sit for months.

### Architecture

```
State DOT APIs                    GOOGLE CLOUD
(GA/FL/NC)     ┌──────────┐  frames  ┌──────────┐ hazard? ┌────────────┐
cameras ───────▶│ WATCHER  │──Pub/Sub─▶│ ANALYST  │─Pub/Sub─▶│ DISPATCHER │──▶ Open311 /
+ events        │ Job      │           │ Service  │         │  Service   │    form / email
                └──────────┘           └────┬─────┘         └─────┬──────┘    (DRY_RUN gate)
                Cloud Scheduler             │ Gemma prefilter     │
                                            │ Gemini (Vertex AI)  ▼
                                       ┌────▼──────┐      ┌────────────┐
                                       │ Firestore │◀─────│  AUDITOR   │
                                       │  cases    │      │    Job     │
                                       └────┬──────┘      │ re-verify  │
                                            │             │ + escalate │
                                       ┌────▼──────┐      └────────────┘
                                       │ DASHBOARD │
                                       │Cloud Run  │
                                       └───────────┘
```

Full detail in [docs/architecture.md](docs/architecture.md).

---

## Project layout

```
src/road_cleaner/
  domain/        Pure logic: models, confidence gate, SLA, narrative. No I/O.
  ports/         Protocol interfaces — one per external dependency.
  adapters/      Two implementations of each port: local and cloud.
  agents/        Watcher, Analyst, Dispatcher, Auditor + the ADK coordinator.
  jurisdiction/  Rules engine mapping road + location → responsible agency.
  pipeline/      The asyncio supervisor that drives all four agents.
  web/           FastAPI dashboard (Jinja templates, no build step).
  cli.py         road-cleaner {doctor,seed,demo,run,audit,serve,cases,outbox}
seeds/           Camera registry, agency registry, scenario timeline.
tests/           unit/ (fast, pure) + integration/ (full pipeline, zero creds).
deploy/          Dockerfile, deploy.sh, teardown.sh.
```

The UI was built from a set of design comps that are kept locally but gitignored — they're
a design-tool export rather than source. Everything they defined now lives in
`web/static/css/tokens.css`.

The dependency rule: `domain/` imports nothing from `adapters/`. `container.py` is the only
place an adapter is chosen.

---

## Configuration

Every setting is in [`.env.example`](.env.example), commented, and defaults to the
fully-local set. `make doctor` prints what's active and what's missing.

The ones that matter:

| Variable | Default | What it does |
|---|---|---|
| `ROAD_CLEANER_MODE` | `local` | `local` = fixtures/SQLite/in-memory. `cloud` = 511 APIs/Firestore/Pub/Sub. Sets every adapter default at once. |
| `DRY_RUN` | `true` | The anti-spam master switch. While true, nothing reaches a real agency. |
| `USE_ADK` | `false` | `true` runs real Google ADK agents for jurisdiction and report prose. |
| `VISION_PROVIDER` | `auto` | `scripted` or `gemini`. |
| `GEMINI_MODEL` | `gemini-2.5-flash` | **Verify against the current model list before enabling.** |
| `GATE_MIN_CONFIDENCE` | `0.55` | Below this, a detection is discarded outright. |
| `GATE_DUPLICATE_RADIUS_METERS` | `500` | How close an official event has to be for us to stay quiet. |

Individual adapter settings override `ROAD_CLEANER_MODE`, so you can run real Gemini
against local SQLite while developing.

---

## Going live

### 1. Get 511 developer keys

Georgia, Florida and North Carolina all run the **same vendor platform** — verified
directly:

```
https://511ga.org/api/v2/get/cameras?key=<KEY>&format=json
https://fl511.com/api/v2/get/cameras?key=<KEY>&format=json
https://www.drivenc.gov/api/v2/get/cameras?key=<KEY>&format=json
```

All three reject keyless requests with `<Error><Message>Invalid Key</Message></Error>`, so
one client covers all three states — and nothing works until keys arrive. Register at each
site and request a developer key; approval can take days. Details and the endpoints that
have moved are in [docs/data-sources.md](docs/data-sources.md).

Published throttle is **10 calls per 60 seconds per key**, enforced client-side in
`adapters/camera/rate_limit.py`. The API is used only for the camera registry and the
incident feed; snapshots come straight from the image CDN URLs, which aren't throttled.

### 2. Set up Vertex AI

```bash
make setup-cloud
export GOOGLE_CLOUD_PROJECT=your-project
export VISION_PROVIDER=gemini USE_ADK=true
make doctor          # confirms what's still missing
```

### 3. Filing for real

> **`DRY_RUN=false` sends reports to real government agencies.**

This is deliberately awkward. Dry run is a *wrapper* around the real filing channel, not a
separate code path — it runs the genuine compose step and declines to transmit, so what
lands in `data/outbox/` is byte-for-byte what would have been sent. There is no untested
path waiting to surprise you.

Before turning it off: read the composed reports, confirm the jurisdiction resolution is
right, and file only hazards a human has reviewed. The agency contact details in
`seeds/agencies.yaml` are deliberately non-routable `.invalid` addresses — filling in real
ones is a separate, conscious step.

---

## Deploying to Google Cloud

```bash
make deploy PROJECT=your-gcp-project
```

Creates Firestore, a GCS bucket with a 7-day frame lifecycle, Pub/Sub topics with a
dead-letter queue, Secret Manager entries, one Cloud Run service (dashboard) and two Cloud
Run jobs (Watcher every 5 min, Auditor hourly). Idempotent; safe to re-run. `DRY_RUN` stays
on.

```bash
make teardown PROJECT=your-gcp-project   # turn it all off again
```

---

## Testing

```bash
make test        # everything, ~60s, zero credentials
make test-fast   # unit tests only, <1s
```

- **`tests/unit/`** — the confidence gate (every branch), SLA and escalation maths,
  jurisdiction rules, the scene renderer, and the perceptual-hash calibration.
- **`tests/integration/`** — the full pipeline end to end on a frozen clock, then the
  dashboard driven against the database it produced.

The end-to-end test asserts the behaviours the product actually promises: a hazard the
state hasn't posted gets reported; one it *has* posted gets suppressed; one hazard never
produces two reports; a hazard nobody fixes gets escalated then handed to a human; and a
hazard that clears closes with a genuine before/after pair.

Several tests exist specifically to stop bugs found during development from coming back —
a prefilter that silently killed every frame, a correlation key that spawned 1,379 cases
for 12 hazards, and a race that filed the same report twice.

---

## Findings

Things worth knowing that only showed up in the building:

- **Frame differencing cannot detect hazards.** An average perceptual hash sees ordinary
  traffic movement far more strongly than it sees a tyre in a lane — measured, a hazard
  appearing while traffic holds still scores **0**. Using "unchanged frame" as a hazard
  filter would silently discard exactly the frames worth analysing. It's used only to skip
  *identical* frames, capped at ten in a row so a static scene can't hide one forever.
- **The prefilter must have high recall, not high precision.** Letting an ordinary frame
  through wastes a fraction of a cent. Discarding one with a hazard in it loses the hazard
  permanently — nothing downstream can recover it.
- **The three launch states share one API.** GA, FL and NC are the same vendor platform, so
  multi-state scale costs one adapter. SC and TN are *not* — `511sc.org` 404s on that path
  and TDOT SmartWay is a separate application. The PRD's assumption held for three of six.
- **The PRD's endpoints have moved.** NCDOT's `eapps.ncdot.gov/services/traffic-prod/v1/*`
  was deprecated in May 2026, and FL's ArcGIS camera FeatureServer — assumed keyless — now
  returns `Token Required`.
- **Correlation is where duplicate-report spam comes from.** The same tyre gets detected
  dozens of times. Without a camera+hazard correlation key and a recurrence cooldown, one
  hazard became 1,379 cases.

---

## House rules

These are constraints on what the system may do, not features:

- **Quiet by default.** `DRY_RUN=true` everywhere, including in production. Turning it off
  is deliberate and per-case.
- **It would rather miss one.** Two frames, 90 seconds apart, cross-checked against the
  state's own feed, against a bar that scales with severity. A robot that cries wolf at a
  maintenance crew is worse than no robot.
- **Escalation stops.** Two filings, then a human. An agent that politely re-sends forever
  is spam with better manners.
- **Roads, not people.** Public infrastructure cameras only. No face or plate analysis,
  ever. Frames deleted after 7 days by bucket lifecycle policy.
- **Rate respect.** Hard client-side governors under each state's published throttle.
- **Auditable.** Every filed report keeps its frames, the model's raw output, the gate's
  reasoning, and timestamps. An agency receiving one could check our work.

## Compliance notes

- All camera data comes from official state DOT developer APIs, which are free, public and
  designed for third-party use.
- **No Google Maps content anywhere in the pipeline** (Maps ToS §3.2.3). If a map view is
  ever added it will use Leaflet with OpenStreetMap tiles.
- Frames are processed transiently; only hazard-positive frames are retained as evidence.

---

Built for the All Things Agentic hackathon (Taskmaster track). Product requirements in
[docs/PRD.md](docs/PRD.md).

Road Cleaner runs on public traffic camera feeds published by state departments of
transportation. It is not affiliated with any of them.
