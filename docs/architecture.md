# Architecture

Diagrams: [docs/diagram.md](diagram.md).

## The problem this shape solves

Two hard constraints drove every structural decision:

1. No Gemini key and no 511 developer keys were available while building.
2. The system files reports with real government agencies, so its decision-making has to be
   testable, reproducible, and conservative.

The answer to both is the same: **every external dependency sits behind a port with a local
implementation, and all decision-making is deterministic Python that a model can inform but
never own.**

## Ports and adapters

| Port | Local | Cloud |
|---|---|---|
| `CameraSource` | Fixture simulator (Pillow-rendered scenes) | `Vendor511CameraSource` (GA/FL/NC) |
| `VisionAnalyzer` | Scripted, scenario-driven | Gemini via Vertex AI (+ Gemma prefilter) |
| `Reasoner` | Scripted | Google ADK `LlmAgent`s |
| `CaseRepository` | SQLite | Firestore |
| `BlobStore` | Local filesystem | Cloud Storage |
| `EventBus` | In-process asyncio queues | Pub/Sub (+ dead-letter topic) |
| `FilingChannel` | Dry-run wrapper → disk | Open311 / maintenance form / SMTP |
| `Clock` | `FrozenClock` | `SystemClock` |

`container.py` is the only module that picks an adapter. Cloud imports are lazy, so a local
install never needs `google-cloud-*` and a missing extra produces an instruction rather
than an ImportError.

### Why `Clock` is a port

Almost everything interesting here is time-shaped: hazards must persist across frames,
reports have deadlines, cases escalate when nobody comes, and cameras are re-checked on a
decaying schedule. Reading the wall clock directly would make all of that untestable and
undemoable — you would wait a real 26 hours to see one escalation. With `FrozenClock`,
sleeping advances time instead of spending it, so `make demo` runs a week in seconds and
the escalation path genuinely executes.

## The four agents

Plain Python classes owning deterministic control flow. They never call each other; they
publish to the bus.

**Watcher** → polls on tiers, hashes, publishes `frame.captured`.
**Analyst** → prefilter, vision, confidence gate; publishes `hazard.confirmed`.
**Dispatcher** → jurisdiction, compose, file, record.
**Auditor** → re-check, close or escalate.

Locally, `pipeline/runner.py` is an asyncio supervisor. On GCP the same classes are Cloud
Run Job entry points (Watcher, Auditor) and Pub/Sub push subscribers (Analyst, Dispatcher).
Only the bus adapter differs.

## Where the model is, and isn't

This is the load-bearing decision in the whole design.

**A model is used for exactly three things**, all genuine matters of judgment:

- reading a camera frame (`VisionAnalyzer`)
- deciding which agency owns a road when the rules cannot (`Reasoner`)
- polishing report prose that was already generated correctly

**Everything else is deterministic Python**: when to poll, whether two frames corroborate,
whether the state already knows, what the SLA is, whether to escalate, whether to file at
all. Those are control flow, and control flow that decides whether to contact a government
agency should be testable, reproducible, and incapable of being talked into something by a
well-phrased frame.

So: **models judge, code decides.** The confidence gate is a pure function with no I/O and
no clock precisely so that every branch through it can be tested exhaustively.

The practical payoff is that `ScriptedReasoner` and `ScriptedVisionAnalyzer` implement the
same interfaces without a model, which is why the whole pipeline runs with no credentials.
Losing the model costs nuance and polish, never correctness.

### ADK

`agents/coordinator.py` exports a `root_agent` — a coordinator with four sub-agents
mirroring the pipeline stages — so the reasoning can be driven from `adk web`. The
production system does not route work by asking a model what to do next; a scheduler tick,
a Pub/Sub message and a confidence gate do that. What the ADK agents own is the reasoning
*inside* a stage.

## The confidence gate

`domain/gating.py`. Four checks in order, each able to stop the process:

1. **Floor** — confidence < 0.55 → drop.
2. **Persistence** — same hazard type, 90s–30min apart, ≥0.50 confidence. None → watch.
3. **Duplicate** — an active official event within 500m whose type overlaps → suppress.
4. **Severity × confidence** — critical 0.60, high 0.70, medium 0.80, low 0.88. Within 0.15
   below the bar → watch. Otherwise → drop.

Returns the decision *and* its reasoning; the reason becomes a line on the case trail, which
is what a human reads to understand why a report was sent.

The bias is one-directional and deliberate: prefer a missed hazard to a false report.

## Case identity

`domain/lifecycle.py`. A camera polled every two minutes will detect the same tyre dozens of
times. Two mechanisms prevent that becoming dozens of reports:

- **`correlation_key(camera_id, hazard_type)`** — identity of an ongoing situation.
- **A 6-hour recurrence cooldown** — closed cases stay findable, so a suppressed hazard
  still on the road doesn't allocate a new case on the next poll.

Plus a per-case lock and a per-tier idempotency check in the Dispatcher, because the bus
runs several workers and two `hazard.confirmed` events for one case can otherwise both see
it as unfiled.

## Dry run is a wrapper, not a mode

`adapters/filing/dry_run.py` wraps a real `FilingChannel`, runs its genuine `compose()`, and
declines to call `transmit()`. Splitting compose from transmit is what makes this possible.

If dry run had its own rendering path, that path would drift, and the first time anyone
flipped `DRY_RUN=false` they would discover that what actually gets sent is not what they
had been reviewing. Here the only difference between a dry run and a live filing is whether
one method is called.

## Data model

```
Camera ──< Frame ──< Detection
   │
   └──< Case ──< TrailEvent
             └──< Filing ──> Agency
```

`Case` holds no presentation state — no colours, no percentages, no formatted timestamps.
Those are computed in `web/serializers.py`, so changing how a case looks never means a
migration.

## Failure handling

- Camera fetch failures are routine, not exceptional — public cameras drop out constantly.
  Logged at debug, the poll moves on.
- A handler that raises does not take down the bus; locally it goes to a dead-letter list,
  on GCP to a dead-letter topic.
- A vision call that fails raises rather than returning "no hazard" — a frame we failed to
  analyse is not a frame with nothing in it.
- An unparseable clearance answer keeps the case open. Closing one wrongly means a real
  hazard stops being watched.
- If jurisdiction can't be resolved, the case is held and flagged, never filed to a guess.

## Cost control

Two mechanisms, in order of how much they save:

1. **The prefilter** — a cheap model kills roughly half of frames before the expensive one
   sees them. It must have high recall; a frame it wrongly discards is lost permanently.
2. **Identical-frame skip** — catches frozen feeds and cached images. Narrow, and capped at
   ten consecutive skips.

Frame differencing is *not* a hazard filter — see the Findings section of the README for the
measurements. Treating it as one would silently discard the frames worth analysing.
