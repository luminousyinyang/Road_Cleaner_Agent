# Devpost submission text

Draft. Paste into the Devpost form; every number here is checkable against the
repo or the running service.

**Track:** Taskmaster
**Live:** https://road-cleaner-dashboard-297023515300.us-central1.run.app
**Repo:** (add URL — if private, share with `testing@devpost.com` and `cloudhackathons@google.com`)

---

## The friction

Everybody drives past the shed truck tyre. It sits in a live lane for
hours because seeing it is not the problem — the problem is everything after:
working out which of eighteen agencies owns that specific stretch, filing on the
channel that agency actually accepts, and then going back to check whether anyone
came. Waze crowdsources sightings and reports them to nobody. The state's own
incident feed only contains what the state already knows.

Road Cleaner closes that loop. Nobody talks to it. Its output is filed reports
with case numbers and a resolution audit trail.

## What it does

Seven stages, per camera, continuously:

```
WATCH → DETECT → CONFIRM → RESOLVE → REPORT → CHECK BACK → PUSH
```

- **Watcher** polls on tiers — busy corridors every 2 minutes, quiet rural
  cameras every 10, any camera with an open case every minute — and skips frames
  identical to the last one, capped at ten in a row so a static scene containing a
  hazard cannot be skipped forever.
- **Analyst** runs vision, then a **confidence gate** that is deliberately pure
  Python with no model in it: a 0.55 floor, a second sighting 90s–30min later,
  a check against the state's own feed within 500m, and a bar that scales with
  severity (critical 0.60, low 0.88). A person on an interstate clears a lower
  bar than debris because the cost of being wrong is not symmetrical.
- **Dispatcher** resolves jurisdiction with rules first and a model only when the
  rules cannot decide, then composes and files through whatever that agency
  accepts — Open311, a maintenance form, or structured email.
- **Auditor** goes back to the same camera and compares against the original
  evidence frame. Cleared closes the case with a before/after pair. Overdue files
  again one tier up. Overdue twice **stops filing** and flags a human.

That last point is the product. Filing and walking away is what every existing
tool does, and it is why potholes sit for months.

## The drill — how you watch it work

You cannot wait for a real mattress to fall off a real truck during a demo. Type
a hazard into the console and the fleet runs the whole thing against it in about
18 seconds: **Gemma 4** turns your sentence into a structured hazard spec,
a camera is invented, two frames are staged four minutes apart, **Gemini 3.7
Flash** analyses *each frame separately*, the real gate decides, **Google ADK**
works out whose road it is, the report is composed — and then it stops.

Invented: the location, the camera, the imagery.
Real: both vision calls, the gate's arithmetic, the agency lookup, the report
text, and the decision about whether it would have been filed at all.

**It cannot file, and that is the point.** A drill case is marked `synthetic`,
excluded from the road log and the public statistics by default, invisible to the
Auditor, and `Dispatcher._file_locked` *raises* rather than returning quietly if
anything tries to file one. The drill only ever calls `compose()`, which the
filing channels guarantee is side-effect free. The UI shows a Send button that
cannot be pressed, and says why.

## Technologies

| | |
|---|---|
| **Gemini 3.7 Flash** (Vertex AI) | hazard vision, clearance verification |
| **Google ADK** | `LlmAgent` + `Runner` — jurisdiction resolution, report prose |
| **Gemma 4** (`gemma-4-26b-a4b-it-maas`) | drill scaffolding, free text → hazard spec |
| **Veo 3.1** | re-stages a confirmed hazard as dashcam footage |
| **Chirp 3 HD** | spoken dispatch briefing |
| **Lyria** | reel score |
| **Cloud Run** | the dashboard and the agent runtime |
| **Firestore / Cloud Storage / Pub/Sub** | adapters written; Firestore is one deploy flag away |
| FastAPI + Jinja2, SQLite, Pillow | one deployable artifact, no build step |

Every external dependency sits behind a Protocol port with two adapters — one
local and credential-free, one Google Cloud — and `container.py` is the only
module that picks. That is why the whole system runs on a clean clone with no
API keys, and why going live is an env-var flip rather than a rewrite.

## Data sources

Public state DOT 511 developer APIs for GA, FL and NC — free, public, and
designed for third-party use. Jurisdiction rules for 18 agencies are hand-built
in `seeds/agencies.yaml` from published district maps. No Google Maps content
anywhere in the pipeline. Camera imagery in the shipped demo is simulated; the
adapters for the real feeds are written and the keys are the only thing missing.

## Findings and learnings

- **A comparison object is still an object.** Telling Veo an object was "never
  larger than a car wheel" did not cap its size — it put a car wheel, chrome rim
  and all, in the middle of the lane. Size clamps now reference only things
  already in the scene.
- **Metadata that reads back is not a capability you have.** A REST `GET` on a
  Vertex publisher model returns 200 for models the project cannot invoke. Every
  `veo-*-generate-preview` id reads fine and 404s when called.
- **A blanket ignore rule shipped a broken image.** `*.md` in `.gcloudignore`
  excluded the Gemini vision prompts, which live in `agents/prompts/*.md`. The
  image built perfectly and died on startup. There is now a test asserting every
  runtime-read file survives the ignore rules.
- **The gate earns its keep in public.** A staged deer produced `animal` on one
  frame and `pedestrian_on_highway` on the other; the gate refused to corroborate
  them and held the case at `watch`. Exactly the outcome it exists to produce.
- **Restraint is a feature you have to show.** An agent that files reports with
  real government agencies is only trustworthy if it visibly declines to file
  the things it should not. The blocked Send button does more for credibility
  than another feature would.

## What is honest about the demo

`DRY_RUN` is on: reports are composed in full, written to disk, and never
transmitted. Generated media is stored apart from camera evidence, badged with
the model that produced it, and cannot enter the evidence chain of a filed
report. Hazards involving a person are never simulated. 300+ tests, several of
which exist only to assert those boundaries hold.
