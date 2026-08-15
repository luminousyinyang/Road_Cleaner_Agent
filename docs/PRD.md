# Road Cleaner — PRD & Design Doc
### Autonomous Road Hazard Detection & Dispatch Agent
> Originally written under the working name *RoadWarden*; renamed to **Road Cleaner**.
> Some assumptions here were overtaken by reality — see `docs/data-sources.md`.

**All Things Agentic Hackathon · Track: The Taskmaster · Deadline: Aug 31, 2026 @ 5:00pm PDT**

---

## 1. Problem Statement

Every day, drivers across the Southeast pass debris, stalled vehicles, damaged guardrails, unmarked lane closures, and washed-out shoulders. They can't do anything about it — they're driving. Even if they could stop, nobody knows *who* to report it to: the same stretch of road might belong to the state DOT, a county public works department, a city, or a toll authority. So hazards sit unreported for hours or days until they cause a crash.

Meanwhile, the infrastructure to see these hazards already exists. State DOTs in FL, GA, NC, TN, AL, and SC operate thousands of public traffic cameras streaming 24/7 — but they're monitored by small Traffic Management Center teams watching hundreds of feeds each, and only during incidents they already know about.

**The gap: detection is passive and reporting is broken.** Waze crowdsources sightings but reports to nobody. DOT cameras see everything but nobody is watching. And no system on earth closes the loop: *see hazard → identify the responsible agency → file the report → track it → verify the fix → escalate if ignored.*

## 2. Solution

Road Cleaner is an autonomous background agent fleet that watches public DOT traffic cameras across the Southeast, detects road hazards the official incident feeds have missed, resolves which agency owns the problem, files the report through the correct channel, and then keeps watching — verifying the hazard was actually cleared and escalating if it wasn't.

It is not a chatbot. Nobody talks to Road Cleaner. It runs 24/7, and its output is *filed reports with case numbers* and a resolution audit trail.

**The core loop (per camera, continuous):**
1. **Watch** — Poll camera snapshots on a smart schedule
2. **Detect** — Gemini 3.5 Flash vision analyzes frames for hazards (debris, stalls, closures, flooding, damaged infrastructure)
3. **Confirm** — Multi-frame + cross-source confidence gating (is it still there 2 frames later? does the official 511 event feed already know about it?)
4. **Resolve** — Jurisdiction agent maps camera location → responsible agency → correct reporting channel
5. **Dispatch** — Files the report (Open311 API where available, structured email/web form elsewhere), captures case/reference number
6. **Track** — Firestore case record; agent re-checks the camera on a decaying schedule to verify clearance
7. **Escalate** — If hazard persists past SLA (e.g., 24h for debris), agent files a follow-up and flags for human review

**Key differentiator vs. Waze/TrafficVision:** they *detect and display*. Road Cleaner *dispatches and tracks*. Detection is commodity; closing the loop is the product.

## 3. Track & Judging Alignment

**Track: The Taskmaster** — "an event-driven workflow with autonomous routing... watching for a change, figuring out what needs to happen next, and interacting with different apps to get the job done, from start to finish."

| Criterion | Weight | How Road Cleaner scores |
|---|---|---|
| Innovation & Operational Utility | 40% | Fully autonomous detect→file→verify loop with zero hand-holding. Removes real friction for two personas: drivers (hazards get fixed) and DOT TMC operators (a tireless second set of eyes on hundreds of feeds). Demo shows real filed reports with real case numbers. |
| Architectural Discipline | 30% | Event-driven Pub/Sub pipeline, decoupled agents (Watcher / Analyst / Dispatcher / Auditor), idempotent processing, per-state rate-limit governors, secrets in Secret Manager, dead-letter queues, confidence gating to prevent false-positive filings. |
| Demo & Production Readiness | 30% | Live unedited demo on real Southeast cameras. Cloud Run dashboard + Vertex AI logs on screen. Clean architecture diagram. One-command deploy (`gcloud run jobs deploy` via script). Reproducible README. |

**Prize targets (in order):** Individual/Hobbyist ($10K ×2 winners — best odds), The Taskmaster ($20K), Best Multimodal UX ($5K ×2 — vision-heavy pipeline qualifies), Grand Prize ($50K).

**Bonus points plan (all three):**
- [ ] Blog post on Medium/dev.to: "I built an AI agent that files pothole reports so you don't have to" — must state it was created for this hackathon
- [ ] LinkedIn post with #AllThingsAgenticHackathon
- [ ] Integrate a second Google model: **Gemma** (run a small Gemma model on Cloud Run as a cheap pre-filter — see §6.3) ✅ hits the "integrate Gemma/Veo/Lyria" bonus

## 4. Hackathon Compliance Checklist

**Required tech (every project must use all three):**
- ✅ Gemini 3.5 Flash via **Vertex AI** (vision analysis + reasoning)
- ✅ Google Agent Framework: **ADK (Agent Development Kit)** — orchestrates the four-agent pipeline
- ✅ Google Cloud infra: **Cloud Run** (agent runtime), **Pub/Sub** (event bus), **Firestore** (case state) — three services where one is required

**Submission requirements:**
- [ ] Category: The Taskmaster
- [ ] Hosted project URL (dashboard on Cloud Run — can be spun down after demo per rules; "does not need to be live at judging")
- [ ] Text description: features, tech, data sources, findings
- [ ] Public repo (or private shared with testing@devpost.com + cloudhackathons@google.com) with **spin-up instructions in README**
- [ ] Architecture diagram
- [ ] ~4-min demo video: problem (30s) → value prop (30s) → live demo (2.5m) → **Google Cloud Console/Cloud Run dashboard/Vertex logs on screen** (30s)

**Data compliance:**
- ✅ All camera data from official state DOT developer APIs (free, public, designed for third parties)
- ❌ **No Google Maps content anywhere in the pipeline** (ToS §3.2.3 prohibits extraction/caching/derived content — and it's a Google-judged hackathon)
- ✅ Frames processed transiently; only hazard-positive frames retained as evidence (small storage footprint, no surveillance angle)
- ⚠️ Real report filing is gated behind `DRY_RUN=true` by default — see §7 (never spam real agencies during dev/testing)

## 5. Data Sources — Southeast Region

Most Southeast 511 systems run the same vendor platform (same API shape as AZ/NY/LA: REST, developer key, JSON/XML, **~10 calls per 60 seconds throttle**). Priority order based on API quality:

| State | System | Access | Notes |
|---|---|---|---|
| **GA** 🥇 | 511ga.org | Free developer key (register account → request key) | REST API: cameras, **GetVideoUrl → m3u8 live streams**, events, alerts, message signs. 10 calls/60s. Best-documented in region. |
| **FL** 🥇 | FL511.com | Free developer key + **public ArcGIS FeatureServer** for camera metadata (no key needed for metadata) | Same platform family as GA. Camera lat/longs queryable via ArcGIS REST. Biggest camera network in the region. |
| **NC** 🥈 | DriveNC.gov / NCDOT | NCDOT public APIs (TIMS incident feed, camera image endpoints) | ~800+ cameras as high-refresh static images (30-60s). NCDOT also publishes Waze-partnership incident data — great cross-reference source. |
| **TN** 🥉 | TDOT SmartWay | Camera endpoints discoverable; no formal dev portal | Stretch goal. Static snapshot URLs. |
| **AL** 🥉 | ALGO Traffic (ALDOT/CAPS UA) | App/site-based; no formal public dev API | Stretch goal. |
| **SC** 🥉 | 511sc.org | Same vendor platform as GA — likely same API | Stretch goal — try GA-style key registration. |

**Build strategy: GA + FL + NC at launch (that's already 2,000+ cameras), AL/TN/SC listed as "architected, pending keys."** Three states fully working beats six states half-working. The multi-state design is what proves scalability to judges — you don't need all six live.

**Action items (do these TODAY — key approval can take days):**
1. Register at 511ga.org → request developer key
2. Register at FL511 developer portal → request key (ArcGIS camera metadata works immediately, no key)
3. Explore NCDOT's API portal (apps.ncdot.gov) for TIMS + camera endpoints
4. Request $150 GCP credits via the hackathon form (Resources tab)
5. Sign up for Google Cloud free trial if not done

**Rate-limit reality check:** 10 calls/60s per key means you can't poll thousands of cameras every minute through the 511 APIs. Design (per §6.2): use the API to fetch the *camera list + image URLs once daily* (metadata changes rarely), then poll the **image CDN URLs directly** for snapshots — these are plain HTTPS image/stream URLs meant for public consumption and aren't behind the API throttle. Use the throttled API only for metadata refresh and event-feed cross-referencing. For GA's m3u8 video streams: URLs are short-lived, so refresh via GetVideoUrl within the rate budget for the handful of "active case" cameras only.

## 6. System Design

### 6.1 Architecture Overview

```
                 ┌─────────────────────────── GOOGLE CLOUD ───────────────────────────┐
                 │                                                                     │
 State DOT APIs  │  ┌──────────┐   frames   ┌──────────┐  hazard?  ┌──────────────┐   │
 (GA/FL/NC ...)──┼─▶│ WATCHER  │──Pub/Sub──▶│ ANALYST  │──Pub/Sub─▶│  DISPATCHER  │   │
 cameras+events  │  │Cloud Run │            │Cloud Run │           │  Cloud Run   │───┼──▶ Open311 /
                 │  │  Job     │            │ Service  │           │   Service    │   │    agency email
                 │  └──────────┘            └────┬─────┘           └──────┬───────┘   │    (DRY_RUN gate)
                 │   scheduler                   │ Gemma pre-filter      │           │
                 │   (Cloud Scheduler)           │ Gemini 3.5 Flash      │           │
                 │                               │ (Vertex AI)           ▼           │
                 │                          ┌────▼─────┐          ┌──────────────┐   │
                 │                          │ Firestore │◀────────│   AUDITOR    │   │
                 │                          │ case DB   │         │ Cloud Run Job│   │
                 │                          └────┬─────┘          │ (re-verify + │   │
                 │                               │                │  escalate)   │   │
                 │                          ┌────▼─────┐          └──────────────┘   │
                 │                          │DASHBOARD │                             │
                 │                          │Cloud Run │  (public demo URL)          │
                 │                          └──────────┘                             │
                 └─────────────────────────────────────────────────────────────────────┘
```

### 6.2 The Four Agents (ADK)

Built as an ADK multi-agent system — a coordinator with four specialized sub-agents. This maps directly to the Aug 11 webinar topic ("Architecting Multi-Agent Teams: the Three Orchestration Patterns of ADK") — **attend it and name-drop the pattern you used in your write-up.**

**① Watcher (Cloud Run Job, triggered by Cloud Scheduler every 2-5 min)**
- Maintains camera registry in Firestore (seeded daily from 511 APIs / ArcGIS)
- Smart polling tiers: high-traffic corridors every 2 min, rural every 10 min, active-case cameras every 1 min
- Fetches snapshot from image CDN URL; computes perceptual hash; **skips publish if frame unchanged** (huge cost saver)
- Publishes `frame.captured` events to Pub/Sub with GCS frame ref

**② Analyst (Cloud Run Service, Pub/Sub push subscriber)**
- **Stage 1 — Gemma pre-filter (bonus points + cost control):** small Gemma vision model served on the same Cloud Run container answers one cheap question: "anything anomalous in this road scene? yes/no." ~80% of frames die here for near-zero cost.
- **Stage 2 — Gemini 3.5 Flash (Vertex AI):** full structured analysis of surviving frames. Prompt returns strict JSON: `{hazard_type, lane_position, severity, confidence, description, visual_evidence}`. Hazard taxonomy: debris | stalled_vehicle | unreported_closure | flooding | infrastructure_damage | animal | pedestrian_on_highway.
- **Stage 3 — Confidence gate (this is your false-positive defense and an explicit talking point for the 30% architecture score):**
  - Requires hazard present in ≥2 frames ≥90s apart
  - Cross-references the state 511 event feed: if the DOT already knows (active event within 500m), suppress — *the whole point is catching what they missed*
  - Severity × confidence matrix decides: file / watch longer / drop
- Confirmed hazards → `hazard.confirmed` event

**③ Dispatcher (Cloud Run Service)**
- **Jurisdiction resolution:** camera metadata (lat/long, road, owning region — the 511 APIs provide owner fields) → rules engine + Gemini reasoning → responsible agency + channel. Registry of Southeast reporting channels built once: state DOT maintenance request forms, Open311 endpoints (several FL/NC cities support it), county public works contacts.
- **Filing:** Open311 POST where available; elsewhere, generates structured email (Gmail API from a dedicated project account) with frame evidence attached, or fills the agency's web form via a headless browser step.
- **`DRY_RUN=true` by default:** writes the fully-formed report to Firestore and a sandbox inbox instead of the real agency. Flip to live only for the small number of real, verified hazards you file during the demo window.
- Captures case/reference numbers → Firestore case record

**④ Auditor (Cloud Run Job, hourly)**
- Re-checks active cases: pulls fresh frames from the case camera, asks Gemini "is the hazard from this evidence photo still present?"
- Cleared → closes case with before/after evidence pair (🔥 demo gold)
- Persisting past SLA → files follow-up, bumps escalation tier, flags on dashboard
- This is the "long-running asynchronous background operation" the entire hackathon theme is about — make the Auditor the star of the write-up

**⑤ Dashboard (Cloud Run Service — the demo surface)**
- Map of Southeast cameras (Leaflet + OpenStreetMap tiles — **not** Google Maps, see §4)
- Live case feed: detection frame → reasoning trace → jurisdiction decision → filed report → verification status
- Reasoning transparency view: show Gemini's actual structured output per case (judges love visible chain-of-thought artifacts)

### 6.3 Tech Stack

| Layer | Choice | Why |
|---|---|---|
| Models | Gemini 3.5 Flash (Vertex AI) + Gemma pre-filter | Required model + bonus-points model; Flash-first is the official cost guidance |
| Agent framework | Google ADK (Python) | Required; best-documented; webinar support |
| Compute | Cloud Run (services + jobs), scale-to-zero, max-instance caps | Required infra; near-zero idle cost |
| Events | Pub/Sub with dead-letter topics | Decoupling story for architecture score |
| State | Firestore (cases, camera registry, audit log) | Required-infra option; simple; free tier generous |
| Frames | Cloud Storage with 7-day lifecycle delete | Evidence retention without cost creep |
| Scheduling | Cloud Scheduler | Polling tiers |
| Secrets | Secret Manager | API keys (511 keys, email creds) — explicit judging criterion ("secure credentials") |
| Dashboard | Next.js or plain React on Cloud Run, Leaflet maps | Fast to build; no Maps ToS risk |
| Observability | Cloud Logging + a simple reasoning-trace log per case | "Handle failures" criterion + demo material |

### 6.4 Cost Plan (target: <$60 of your $150)

- Frame diffing kills ~70% of Gemini calls; Gemma pre-filter kills ~80% of the rest
- Napkin math: 300 active cameras × 20 polls/hr × 24h = 144K frames/day → ~43K post-diff → ~8.6K Gemini Flash vision calls/day ≈ low single-digit $/day at Flash pricing. Run full-fleet mode only during demo-capture week; dev on 20-30 cameras.
- Cloud Run scale-to-zero + max instances = 3; budget alert at $50 and $100
- Turn everything off after recording (per official cost tips); keep the repo deployable

## 7. Ethics & Anti-Spam Guardrails (put this section in your submission — it's a differentiator)

- **DRY_RUN default:** the agent never contacts a real agency in dev/test. Live filing enabled manually, per-case, during the demo window only.
- **Confidence gating:** multi-frame + cross-source confirmation before any filing; conservative thresholds (prefer missed detections over false reports).
- **Human-visible audit trail:** every filed report has stored frames, model reasoning, and timestamps — an agency could audit any report.
- **No surveillance creep:** public infrastructure cameras only, no PII extraction, frames auto-deleted in 7 days, no license plate / face analysis ever.
- **Rate respect:** hard client-side governors under each state's published throttle.

## 8. Three-Week Timeline (solo, ~evenings + weekends, DataHub ships Aug 10)

**Week 1 (Aug 11-17) — Pipeline spine**
- Day 1: GCP project, credits, 511GA/FL511 keys requested (do NOW, approval lag), ADK hello-world on Cloud Run
- Day 2-3: Watcher — FL ArcGIS + GA camera registry into Firestore, snapshot polling, perceptual-hash diffing, Pub/Sub publish
- Day 4-5: Analyst — Gemini 3.5 Flash structured hazard detection working on real frames; build a 50-frame eval set (hand-label frames you collect: clear road / debris / stall / closure) and measure precision — **put the eval numbers in your write-up**
- Weekend: Confidence gate + 511 event cross-reference; attend Aug 11 ADK webinar recording; Gemma pre-filter
- *Milestone: real GA/FL camera → confirmed hazard event in Pub/Sub*

**Week 2 (Aug 18-24) — Dispatch + audit loop**
- Day 1-2: Jurisdiction registry for GA/FL/NC + Dispatcher with DRY_RUN filing (Open311 + email templates)
- Day 3-4: Auditor re-verification loop + escalation tiers; NC (DriveNC) onboarding
- Day 5-weekend: Dashboard (map, case feed, reasoning traces); end-to-end DRY_RUN soak test over a full weekend — collect real detections continuously (this soak run is where your demo footage comes from)
- *Milestone: untouched 48-hour run producing confirmed, dispatched (dry-run), audited cases*

**Week 3 (Aug 25-31) — Demo, polish, submit**
- Day 1-2: Architecture diagram (Excalidraw/draw.io), README spin-up guide, deploy script
- Day 3: Pick 2-3 REAL hazards from the soak run, flip DRY_RUN off, file genuine reports, capture case numbers
- Day 4: Record 4-min demo: 0:00 problem → 0:30 value prop → 1:00 live dashboard with real detections → 2:30 a filed real report + case number → 3:00 before/after clearance verification → 3:30 Cloud Run console + Vertex logs on screen
- Day 5: Blog post + LinkedIn post (#AllThingsAgenticHackathon), text description, findings/learnings
- Day 6 (Aug 30): Submit. Never submit deadline day. Day 7: buffer.

## 9. Risks & Mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| 511 API keys delayed | Med | Request day 1; FL ArcGIS metadata needs no key; NCDOT endpoints public |
| Vision false positives (shadows, rain, night) | High | Confidence gate; conservative thresholds; eval set with measured precision; demo cameras chosen for image quality |
| Hazards are rare → nothing to demo | Med | 48h+ soak across 300+ cameras WILL catch stalls/debris (they're constant on FL/GA interstates); fallback: severity tier includes common events like shoulder stalls |
| Image CDN URLs unstable / streams expire | Med | Daily registry refresh; m3u8 refresh logic with backoff (known GA pattern); snapshot URLs are the primary path, video is garnish |
| Filing real reports goes wrong | Low | DRY_RUN default; only file human-reviewed real hazards; keep it to 2-3 filings |
| Cost blowout | Low | Diff + Gemma pre-filter + budget alerts + scale-to-zero + off-after-demo |
| Scope creep to 6 states | High (it's you) | GA+FL+NC hard scope; others are registry entries marked "pending key" to prove the architecture scales |

## 10. Findings/Learnings Section (pre-plan it — it's a submission requirement)

Track these as you build, they write the section for you:
- Measured Gemini Flash hazard-detection precision/recall on your eval set
- % of frames killed by diffing and by Gemma pre-filter (cost story)
- Detection latency: hazard appears → report filed
- Anything the agent caught that the official 511 feed missed (THE headline stat)

## 11. One-liner for the Devpost gallery

> **Road Cleaner** — an autonomous agent fleet that watches 2,000+ Southeast DOT traffic cameras 24/7, spots the hazards official feeds miss, figures out which agency owns the road, files the report, and keeps watching until it's actually fixed.
