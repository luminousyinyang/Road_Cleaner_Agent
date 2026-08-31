# Road Cleaner

**Point your phone at the road and drive. Road Cleaner spots the hazard, works out which
of 69 agencies owns that stretch, and files the report on the channel that agency actually
accepts.**

Detection is the easy part. Plenty of things can spot a pothole. The hard part is
everything after seeing it — working out whose road it is (Bellevue maintains its own
streets; the state DOT owns only the routes running through them), saying so through the
channel that agency accepts, and doing it without pulling over to fill in a form from
memory an hour later. So nobody does. Waze crowdsources sightings and reports them to
nobody.

Road Cleaner removes the whole tax. Nobody talks to it — it runs on a loop rather than on
a prompt, and its output is *filed reports* with a copy in your inbox as the receipt.

The same four-agent pipeline also reads traffic-camera feeds without a driver, and you can
watch all four run on a hazard you describe at [`/drill`](#the-drill--watch-the-four-agents-work).

And having found a hazard, it can do one more thing with it. An autonomous driving stack
sees a million miles of ordinary road and almost no shed truck tyres — rare events are
precisely what it is short of, and precisely what a road full of dashcams sees all day.
So once a hazard is confirmed, Road Cleaner can re-stage it from a dashcam view with Veo
and hand back the footage that was missing. Real detection, synthetic footage, clearly
labelled as such: see [Simulation](#simulation--synthetic-footage-from-real-hazards).

---

## For hackathon judges

Built for the **All Things Agentic Hackathon** — track: **Taskmaster**.

| | |
|---|---|
| **Live service** | https://road-cleaner-dashboard-6yx6cifega-uc.a.run.app |
| **Spin-up guide** | [Quickstart](#quickstart--no-credentials-no-cloud-account-two-commands) — two commands, no credentials |
| **Architecture diagram** | [`docs/diagram.md`](docs/diagram.md) · PNGs in [`docs/img/`](docs/img/) |
| **Deploy to your own project** | [`make deploy PROJECT=…`](#deploying-to-google-cloud) |
| **Submission write-up** | [`docs/submission.md`](docs/submission.md) |

Required tech, and where to find each one in the source:

| Requirement | Used | Where |
|---|---|---|
| Gemini 3.5+ via Vertex AI | `gemini-3.7-flash` | [`adapters/vision/gemini_vision.py`](src/road_cleaner/adapters/vision/gemini_vision.py) |
| A Google agent framework | **Google ADK** — `LlmAgent`, `Runner` | [`adapters/reasoning/adk_reasoner.py`](src/road_cleaner/adapters/reasoning/adk_reasoner.py), [`agents/coordinator.py`](src/road_cleaner/agents/coordinator.py) |
| A Google Cloud service | **Cloud Run** + Firebase Auth, both live | [`deploy/deploy.sh`](deploy/deploy.sh), [`web/auth.py`](src/road_cleaner/web/auth.py) |
| *Bonus:* other Google models | Gemma 4, Veo 3.1 (pipeline) · Chirp 3 HD, Lyria | [`adapters/media/`](src/road_cleaner/adapters/media/), [`pipeline/drill.py`](src/road_cleaner/pipeline/drill.py) |

Firestore, Cloud Storage and Pub/Sub adapters are written and tested, but the
deployed revision runs SQLite, local disk and an in-process bus —
`deploy.sh --with-firestore` switches the first two. Said plainly here because
"uses Google Cloud" should mean something specific.

`root_agent` is exported from [`agents/coordinator.py`](src/road_cleaner/agents/coordinator.py),
so the agent team can be driven directly from `adk web`.

---

## Quickstart — no credentials, no cloud account, two commands

```bash
make setup     # venv + dependencies + .env
make demo      # watch a simulated week of roads, then open the dashboard
```

Then visit **http://127.0.0.1:8080**.

> **`make demo` wipes `data/` first.** It re-runs the simulated week from scratch,
> which means the cases you were looking at get new ids — and any generated clips
> from the old run are orphaned, because they are keyed by case id. Use
> `road-cleaner demo --no-reset` to keep what you have, or take a copy of
> `data/road_cleaner.db` first. (`python deploy/bundle.py` makes exactly such a
> copy, alongside the frames the cases reference.)

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
make test      # the whole suite — 900+ tests, still no credentials
make outbox    # the reports that would have been sent
make clean     # delete all generated data
```

---

## What actually runs today

Being precise about this, because "it works" is doing a lot of work in most READMEs:

| | Status |
|---|---|
| **Live dashcam** | **Real, deployed, and the product.** A phone, Gemini on every frame, a report to the agency that owns that road. |
| **Reporting to a real agency** | **Real.** Bellevue's published 24-hour maintenance desk, behind two switches — see [Going live](#going-live). |
| Four-agent pipeline, seven-stage loop | **Real.** Runs end to end; ~140 tests. Watch it at [`/drill`](#the-drill--watch-the-four-agents-work). |
| Confidence gate, SLA, escalation, jurisdiction rules | **Real.** Pure logic, exhaustively tested. |
| Dashboard, case detail, audit trail, evidence frames | **Real.** Server-rendered from the database. |
| Google ADK | **Real.** Resolves jurisdiction when the rules cannot, and polishes the report. |
| Vision analysis | **Gemini on Vertex.** Scripted analyzer is the credential-free default; flip `VISION_PROVIDER=gemini`. |
| Traffic-camera feeds | **Simulated.** `CAMERA_SOURCE=fixture` renders road scenes. The 511 adapter is written and tested; **no developer key has ever been set**, so it has never run against a live feed. |
| Check-back / clearance | **Real, on demand.** `POST /api/cases/{id}/recheck` runs the Auditor now. There is no scheduled job doing it unattended. |
| Watcher + Auditor as scheduled jobs | **Written, not deployed.** `deploy.sh --with-fleet` creates them; the live project was deployed without it, so `gcloud run jobs list` returns nothing. Runs locally via `make demo`. |
| Report filing — camera pipeline | **Dry run.** Composed in full, written to `data/outbox/`, and `transmit()` is not called. The dashcam path is different and *does* send — see the row above and [Going live](#going-live). |
| Simulation (Veo / Chirp / Lyria) | **Real, and off by default.** Generated clips are in `data/media/`. Costs money per second of video. |
| Cloud Run + Firebase Auth | **Deployed and verified.** |
| Firestore / GCS / Pub/Sub | **Written, not enabled on the deployed revision**, which runs SQLite, local disk and an in-process bus. `--with-firestore` flips the first two. Pub/Sub has no consumer yet. |

The whole design exists to make that gap an env-var flip rather than a rewrite. Every
external dependency sits behind a port with two implementations.

---

## How it works

Seven stages, per camera, on a loop:

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

Rendered diagrams — the fleet, one drill end to end, and the evidence/generated
boundary — are in [docs/diagram.md](docs/diagram.md). Full prose in
[docs/architecture.md](docs/architecture.md).

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
  web/           FastAPI dashboard (Jinja, no build step). `/` is the scenario
                 library; also `/cases/{id}`, `/dashcam` and `/incidents`.
                 /log and /simulation redirect.
  cli.py         road-cleaner {doctor,seed,demo,run,audit,serve,cases,outbox,simulate}
seeds/           Camera registry, agency registry, scenario timeline.
tests/           unit/ (fast, pure) + integration/ (full pipeline, zero creds).
deploy/          Dockerfile, deploy.sh, teardown.sh.
data/frames/     Camera evidence.   ─┐ separate on purpose: see Simulation.
data/media/      Generated clips.   ─┘
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
| `GEMINI_MODEL` | `gemini-3.7-flash` | Verified on Vertex 2026-08-18. Introductory pricing ends 2026-12-31, when it doubles to $1.50/$7.50 per 1M tokens — worth re-checking then. |
| `GEMMA_PREFILTER_ENABLED` | `false` | Needs a self-deployed Model Garden endpoint — Gemma is not serverless on Vertex. |
| `MEDIA_PROVIDER` | `scripted` | `vertex` really calls Veo/Chirp/Lyria and really bills. Does **not** follow `ROAD_CLEANER_MODE`. |
| `VEO_MODEL` | `veo-3.1-fast-generate-001` | Use a GA id; every `-preview` id 404s on invocation. |
| `VERTEX_MEDIA_LOCATION` | `us-central1` | Veo and Lyria are region-pinned and are not served from `global`. |
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

Bare, that deploys **one Cloud Run service** — the dashboard, the drill and the live
dashcam. Idempotent; safe to re-run. `DRY_RUN` stays on.

The rest is behind two flags, and neither is on by default:

```bash
./deploy/deploy.sh PROJECT --with-firestore   # Firestore + GCS + their indexes
./deploy/deploy.sh PROJECT --with-fleet       # Watcher (5 min) + Auditor (hourly) jobs,
                                              # Pub/Sub topics, Cloud Scheduler
```

**Without `--with-fleet` there is no fleet in the cloud** — no Cloud Run jobs, no
schedules, nothing polling. The dashboard still serves everything you can drive by
hand, and the fleet still runs end to end locally via `make demo` and `make run`.
Worth stating outright, because "an agent fleet running continuously" is checkable
with one `gcloud run jobs list`.

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

## The drill — watch the four agents work

You should not have to wait for a real mattress to fall off a real truck to see
whether an agent works. Go to **[`/drill`](#the-drill--watch-the-four-agents-work)**,
describe a hazard, and the fleet runs the whole thing against it in about twenty
seconds.

It has a page of its own rather than a slot on the front door, deliberately: that
page is a library of hazards the fleet actually found, and a box that invents one
is a different product standing in the middle of it.

| Stage | What actually runs | Model |
|---|---|---|
| **Scaffold** | free text → state, road, direction, lane, hazard type, county | **Gemma 4** |
| **Stage** | invent a camera; render two frames four minutes apart | — |
| **Detect** | analyse **each frame separately** | **Gemini 3.7 Flash** |
| **Confirm** | the real `domain/gating.evaluate()` | — |
| **Resolve** | `jurisdiction.resolve()` | **Google ADK** |
| **Report** | `narrative.report_body()` + `channel.compose()` | — |
| **Push** | **blocked** — draft only | — |

About 18 seconds locally, under a minute on Cloud Run.

**What is invented:** the location, the camera, the imagery.

**What is real:** both vision calls, the gate's arithmetic, the agency lookup,
the report text, and the decision about whether it would have been filed at all.

Two frames rather than one is deliberate. The gate exists to require two
independent observations 90s–30min apart before a case opens, and two real model
calls on two real moments satisfy that honestly — only the clock is invented.
Fabricating a second detection row to clear the gate would defeat the single
check the whole system is built around.

The drill has caught the gate doing its job on camera: a staged deer produced
`animal` on one frame and `pedestrian_on_highway` on the other, the gate refused
to corroborate them, and the case stayed at `watch`.

### It cannot file, and that is the point

A drill invents a location, so there is no road to report and no agency that
should hear about it. Five things enforce that rather than intend it:

1. `Case.synthetic` is set, and the id carries a `SIM-` prefix.
2. `Dispatcher._file_locked` **raises** on a synthetic case — a silent skip would
   be indistinguishable from "nothing to file".
3. The drill only ever calls `compose()`, which the filing channels guarantee is
   side-effect free. `transmit()` is never reached.
4. Synthetic cases are excluded from the road log, `/api/stats` and the Auditor's
   queue **by default**, so no caller has to remember to filter them out.
5. Drill frames go to the media store, never to the evidence store behind
   `/frames/`.

Tests assert each one.

---

## Simulation — synthetic footage from real hazards

The detection pipeline is a scenario miner. It watches public cameras for the road events
an AV perception stack rarely sees, and every confirmed case is a record of one that
actually happened — where, on what road, in which lane, described by the analyst.

`road-cleaner simulate` takes that record and asks **Veo** to show the same event from a
dashcam instead of a pole-mounted camera, which is the perspective a perception stack
trains on. **Chirp 3 HD** reads the dispatch briefing aloud. **Lyria** scores the reel.

```bash
road-cleaner simulate --dry-run                    # print the prompts, generate nothing
road-cleaner simulate --case GA-4462 --provider vertex
road-cleaner simulate --provider vertex --narrate --score
road-cleaner serve                                 # the scenario library is the home page
```

You can also render from the dashboard. Every confirmed case appears in the scenario
library at `/`: with its clip if one exists, with a **Generate dashcam clip** button if not,
and — for hazards involving a person — with a card explaining that it will never have one.
That is why the library replaced the old road log rather than sitting beside it: it lists
every case, including the ones that never produced footage.

The generate button is disabled unless `MEDIA_PROVIDER=vertex`, states the cost beside it,
and allows one render per case at a time so a double-click cannot bill twice.

The progress bar is **an estimate and says so**. Veo reports no percentage — an operation
is running or it is done — so the bar is elapsed time against a typical render, capped
below full while still going. A bar animating to 99% would look better and would be
telling you something it does not know.

Generation never happens in the polling loop and never as a side effect of loading a page;
it always takes an explicit command or click. A render is a long-running operation billed
per second of video and takes roughly **two minutes at 1080p**.

### The line this must not cross

Generated footage is not evidence, and the system is built so that cannot blur:

- Clips are stored in `data/media/`, **not** `data/frames/`, and keyed under `synthetic/`.
- Re-rendering a case **replaces** its previous clip rather than piling another 20MB beside
  it. Only media of the same kind is pruned, so a new video never deletes the spoken
  briefing, and pruning is scoped to one case's own folder. Nothing analogous exists for
  `data/frames/`: generated clips are regenerable by definition and evidence is not.
- Every clip is written with a `.json` provenance sidecar naming the model, the prompt and
  the case it came from. The UI badge reads from that sidecar rather than guessing.
- `/media` refuses to serve anything outside `synthetic/`; `/frames` serves evidence. A
  test asserts an evidence key 404s on `/media`.
- Nothing generated can reach a filed report. A test walks the outbox to confirm it.
- **Hazards involving a person are never simulated** — `pedestrian_on_highway` is refused
  by name, not left to the safety filter.

A hazard report backed by generated footage would be a fabricated record. The value of
this project rests on its reports being checkable, so the separation is the point rather
than a precaution.

### Notes from wiring it up

- **Gemma does not work on Vertex.** Not as a serverless publisher model — every variant
  404s. It needs a self-deployed Model Garden GPU endpoint. `doctor` warns if you enable
  the prefilter without one.
- **A 200 from a publisher-model `GET` does not mean you can call it.** Every
  `veo-*-generate-preview` id reads back fine over REST and 404s on invocation. Only the
  GA ids (`veo-3.1-generate-001`, `veo-3.1-fast-generate-001`) work.
- **Veo is region-pinned.** It is not served from `global`, so `VERTEX_MEDIA_LOCATION` is
  a separate setting from `GOOGLE_CLOUD_LOCATION`.
- **Do not seed image-to-video from fixture frames.** They are flat-shaded synthetic
  renders with the camera id and timestamp burned in, and Veo faithfully reproduces both —
  you get a cartoon with doubled overlay text. `--seed-frame` is off by default and worth
  turning on only against a real 511 feed.
- **Safety filtering is a real failure mode.** Damaged-guardrail prompts get refused.
  Describing hazards plainly rather than dramatically gets most of them through.
- **Lyria writes music, not sound effects.** The road noise and sirens come from Veo's own
  `generate_audio`.
- **Veo has a small per-minute quota.** Generating several clips back to back returns 429.
  The adapter says so in plain words rather than dumping the error.

### Getting the footage to look real

The first pass looked wrong in a specific way: a shed tyre tread rendered as a row of
car-sized black masses, and an animal ballooned into a morphing blob. Three fixes, in
descending order of how much they mattered:

1. **Anchor the physical size in the prompt.** "A piece of debris" gives the model nothing
   to scale against, so it reaches for drama. A stated dimension — "a torn scrap of black
   rubber, roughly 40 centimetres long, lying completely flat on the asphalt" — does not.
   Every hazard type in `scenario_prompt.py` carries an anchor like this plus a size clamp
   written for that hazard, because "never larger than a car wheel" is a sensible limit for
   tyre debris and nonsense for a stalled sedan or a sheet of standing water.
2. **Use the real `negative_prompt` field.** Putting "no collision" in the prompt *text* is
   close to useless — it reads as a mention of a collision. The dedicated parameter is a
   genuine exclusion, and it is where the scale words belong.
3. **Do not feed the analyst's prose in unfiltered.** Detection descriptions say things
   like "Large dark object, likely shed truck tyre tread" — and "large" is exactly the
   instruction that oversized everything. Descriptions carrying scale words are dropped.

**A comparison object is still an object.** This one cost two rounds of renders. The clamp
"never larger than a car wheel" did not cap the size of the debris — it put an intact car
wheel, chrome rim and all, in the middle of the lane. "Normal guardrail height" produced a
guardrail towering over the bonnet. The model renders every noun you write, including nouns
you only meant as a measuring stick. So the clamps now refer exclusively to things already
in the scene — the lane, the passing traffic, the road surface — and the debris phrasing
avoids the words "tyre" and "wheel" entirely.

**Contradictions get resolved the wrong way.** The placement sentence used to say "directly
ahead in the car's own lane" for every hazard, including a *roadside* damaged barrier. Veo
settled that contradiction by swinging the barrier out into the carriageway. Placement is
now per-hazard: roadside things stay at the roadside.

**The analyst's prose can be dramatic as well as oversized.** "Rail end protruding toward
the travel lanes" is accurate incident prose and a terrible instruction. The description
filter drops escalation language ("protruding", "torn open", "blocking") alongside the size
words.

Two more that helped: naming the road made Veo paint gantry signage, and generated signage
comes out garbled (one clip read "Howell Mill Road Road"), so the prompt describes the road's
*character* instead. And `resolution="1080p"` is worth the extra render time.

**`enhance_prompt` cannot be turned off.** Veo rewrites your prompt before rendering and
reaches for cinema doing it, which is the root of the drama problem. Setting it to `False`
fails the request outright: *"Veo 3 prompt enhancement cannot be disabled."* The size
anchors and the negative prompt are the only counterweights available.

## When the model says no

Vertex throttles, and an agent that treats a 429 as a detection failure is
worse than useless -- it silently discards the frame and the hazard with it.

A full-speed run taught this the hard way: the Analyst handles every
`frame.captured` event as it arrives, so a busy tick fired hundreds of
concurrent vision calls. Vertex refused nearly all of them. The measured result
was **165 consecutive `429 RESOURCE_EXHAUSTED` and zero detections** — the
pipeline ran to completion and produced nothing, without ever failing loudly.

So `GeminiVisionAnalyzer` now:

- holds a **semaphore** (`VISION_MAX_CONCURRENCY`, default 4) so the number of
  in-flight calls is bounded no matter how many frames arrive at once;
- **retries transient failures** with exponential backoff and jitter — 429, 503,
  504 mean "not now" and the frame is still good;
- **does not retry** a 400 or a 404, because repeating a malformed request just
  spends money on the same mistake;
- **sleeps outside the semaphore**, so a backing-off call is not holding a slot
  another frame could use;
- and still raises rather than returning "no hazard" when it finally gives up. A
  frame we failed to analyse is not a frame with nothing in it.

The jitter matters more than it looks: without it, every worker throttled at the
same instant retries at the same instant, and the second wave collides exactly
like the first.

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
- **Generated is never evidence.** Synthetic media is stored apart, badged with the model
  that made it, and cannot enter the evidence chain of a filed report. Hazards depicting a
  person are not generated at all.

## Compliance notes

- All camera data comes from official state DOT developer APIs, which are free, public and
  designed for third-party use.
- **No Google Maps content anywhere in the pipeline** (Maps ToS §3.2.3). If a map view is
  ever added it will use Leaflet with OpenStreetMap tiles.
- Frames are processed transiently; only hazard-positive frames are retained as evidence.
- Synthetic media generated by Veo, Chirp or Lyria is labelled as generated wherever it is
  shown, and is never presented as camera footage or attached to a report.

---

Built for the All Things Agentic hackathon (Taskmaster track). Product requirements in
[docs/PRD.md](docs/PRD.md).

Road Cleaner runs on public traffic camera feeds published by state departments of
transportation. It is not affiliated with any of them.
