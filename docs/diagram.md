# Architecture diagram

Mermaid rather than an exported image: it renders on GitHub, it diffs in review,
and it cannot drift out of date behind a PNG nobody regenerated.

PNGs are generated *from* these blocks and live in [`img/`](img/), for places
that cannot render mermaid. They are a build artifact of this file, never the
source — regenerate them rather than editing them:

```bash
make diagrams        # docs/diagram.md -> docs/img/*.png
```

| | |
|---|---|
| [The system](img/01-system.png) | The four-agent fleet, end to end |
| [The dashcam path](img/02-dashcam.png) | A phone on a windscreen |
| [One drill](img/03-drill.png) | What runs when you type a hazard |
| [Deployment](img/04-deployment.png) | What runs where on Google Cloud |
| [The boundary](img/05-boundary.png) | Why synthetic media can never be filed |

## The system

Two ways a frame gets in, one pipeline behind them. The **live dashcam** is the
deployed product and what the demo shows; the agent fleet is the same pipeline
reading a camera source instead of a phone.

Be clear about that source, because the diagram used to overstate it: it is a
**fixture** that renders road scenes. The `Vendor511` adapter for the real GA /
FL / NC feeds is written and tested, but no developer key has ever been set, so
it has never run against a live feed. Everything downstream of the frame — the
gate, the jurisdiction registry, the report, the refusals — is the same code on
both paths.

```mermaid
flowchart TB
    subgraph sources["Where a frame comes from"]
        phone["📱 <b>Live dashcam</b><br/>a phone on a windscreen<br/><i>deployed · the product</i>"]
        cams["Camera source<br/><i>fixture — renders road scenes</i><br/>511 adapter written, no key"]
    end

    subgraph agents["Agent fleet"]
        watcher["<b>Watcher</b><br/>polls on tiers<br/>skips identical frames"]
        analyst["<b>Analyst</b><br/>prefilter → vision → gate"]
        dispatcher["<b>Dispatcher</b><br/>jurisdiction → compose → file"]
        auditor["<b>Auditor</b><br/>re-checks a case<br/>escalates twice, then stops"]
    end

    subgraph models["Google AI"]
        gemma["<b>Gemma 4</b><br/>gemma-4-26b-a4b-it-maas<br/><i>drill scaffold</i>"]
        gemini["<b>Gemini 3.7 Flash</b><br/>Vertex AI<br/><i>hazard vision</i>"]
        adk["<b>Google ADK</b><br/>LlmAgent + Runner<br/><i>jurisdiction, report prose</i>"]
        veo["<b>Veo 3.1</b><br/><i>dashcam re-staging</i>"]
    end

    subgraph gate["The confidence gate — pure Python, no model"]
        g["1 · floor 0.55<br/>2 · two frames, 90s–30min apart<br/>3 · not already in the state feed<br/>4 · severity × confidence"]
    end

    dedup["<b>24h duplicate check</b><br/>same hazard, within 500m,<br/><i>across every user</i>"]
    jur["<b>Jurisdiction registry</b><br/>69 agencies · rules first"]

    subgraph state["State"]
        repo[("Firestore / SQLite<br/>cases · detections · trail")]
        incidents[("Incident store<br/><i>scoped by uid</i>")]
        blobs[("Cloud Storage / disk<br/>evidence frames")]
        media[("media store<br/><i>generated — kept apart</i>")]
    end

    agencies["Agency desks<br/>Open311 · form · email"]

    %% the dashcam path — see 02-dashcam for its refusals in full
    phone --> gemini
    phone -->|"only what you press the button on"| dedup
    dedup -->|"already reported — kept, not sent"| incidents
    dedup -->|first report| jur

    %% the camera path
    cams --> watcher --> analyst
    analyst --> gemini
    analyst --> gate
    gate -->|watch / suppress| repo
    gate -->|file| dispatcher --> jur
    dispatcher --> auditor --> repo

    %% shared from here down
    jur -.->|"only when the rules cannot decide"| adk
    jur --> agencies
    jur --> incidents
    watcher --> blobs
    repo --> veo --> media

    classDef google fill:#1D4E6B,stroke:#0E1116,color:#EAF3F9
    classDef store fill:#F2F5F8,stroke:#94A5B4,color:#1A1A18
    classDef guard fill:#FBEDE7,stroke:#B4451F,color:#1A1A18
    classDef live fill:#1F5C3D,stroke:#0E1116,color:#EAF3F9
    class gemma,gemini,adk,veo google
    class repo,blobs,media,incidents store
    class g,dedup,noloc guard
    class phone live
```

## The second way in — a phone on a windscreen

The fleet above watches public cameras on a schedule. This is the other input:
a person driving, with the same model, the same gate vocabulary and the same
jurisdiction registry behind it. Nothing is stored unless they press the button.

```mermaid
flowchart TB
    phone["📱 Phone camera<br/><i>a frame every 2.5s</i>"]

    subgraph browser["Browser — nothing kept here"]
        pump["Look scheduler<br/>up to 6 in flight<br/><i>sequence-ordered, stale replies dropped</i>"]
        modal["Find dialog<br/><i>picture + box + countdown</i>"]
        geo["Geolocation<br/><i>required before the camera opens</i>"]
    end

    look["<b>POST /api/dashcam/look</b><br/>20s deadline · nothing written"]
    gemini2["<b>Gemini 3.7 Flash</b><br/>Vertex AI"]

    subgraph keep["Only if the button is pressed"]
        dedup["<b>24h duplicate check</b><br/>same hazard family, within 500m,<br/>across <i>all</i> users"]
        juris["Jurisdiction registry<br/><i>city service area → state DOT</i>"]
        store[("Incident store<br/>Firestore / disk<br/><i>scoped by uid</i>")]
        mail["Email channel<br/><i>reporter + agency</i>"]
    end

    auth["Firebase Auth<br/><i>verified ID token → uid</i>"]

    phone --> pump --> look --> gemini2
    gemini2 -.->|hazard + box| modal
    geo --> pump
    modal -->|"Report it now"| dedup
    auth -.->|uid| dedup
    dedup -->|first report| juris --> mail
    dedup -->|"already reported"| store
    juris --> store

    classDef google fill:#1D4E6B,stroke:#0E1116,color:#EAF3F9
    classDef store fill:#F2F5F8,stroke:#94A5B4,color:#1A1A18
    classDef guard fill:#FBEDE7,stroke:#B4451F,color:#1A1A18
    class gemini2,auth google
    class store store
    class dedup guard
```

Three things on that path are worth naming, because they are where the judgement
lives rather than the plumbing:

* **Every frame is discarded.** `/api/dashcam/look` writes nothing at all — no
  frame, no detection, no case. Only a find somebody actively reports is kept.
* **The duplicate check crosses users.** One pothole driven past by forty people
  is one email, not forty. It reads a projection — hazard type, coordinates,
  timestamp — never anybody's photograph or address.
* **A report needs coordinates or it does not go.** The camera will not open
  without location permission, because a crew cannot be sent to "somewhere".

## One drill, end to end

What runs when you type a hazard into the console. Every box except the first
two is the same code a real camera detection goes through.

```mermaid
sequenceDiagram
    autonumber
    actor You
    participant Drill as Drill
    participant Gemma as Gemma 4
    participant Scene as Scene renderer
    participant Gemini as Gemini 3.7
    participant Gate as Confidence gate
    participant ADK as ADK reasoner
    participant Channel as Filing channel

    You->>Drill: "a mattress in the fast lane on I-85"
    Drill->>Gemma: turn this into a hazard spec
    Gemma-->>Drill: state, road, lane, hazard type, county
    Note over Drill: camera invented with NO owner agency —<br/>so the rules cannot shortcut, and ADK must think

    Drill->>Scene: render two frames, 4 min apart
    Drill->>Gemini: analyse frame 1
    Gemini-->>Drill: debris · 0.95
    Drill->>Gemini: analyse frame 2
    Gemini-->>Drill: debris · 0.95
    Note over Drill,Gemini: two independent calls, not one call counted twice

    Drill->>Gate: evaluate(second, priors=[first])
    Gate-->>Drill: FILE — two frames agree at 0.95
    Drill->>ADK: whose road is this?
    ADK-->>Drill: Georgia DOT — District 7
    Drill->>Channel: compose(report)
    Channel-->>Drill: rendered report

    rect rgb(251, 237, 231)
        Note over Drill,Channel: STOP. compose() only — transmit() is never called,<br/>and the case is marked synthetic, which the<br/>Dispatcher refuses to file outright.
    end
    Drill-->>You: the report, and a Send button that cannot be pressed
```

## What runs where, on Google Cloud

The deployed shape. Every box marked *port* has a second implementation that
runs on a laptop with no credentials, which is why `make demo` works on a clean
clone — see [architecture.md](architecture.md).

```mermaid
flowchart TB
    user["Driver / dashboard viewer"]

    subgraph gcp["Google Cloud — project road-cleaner"]
        run["<b>Cloud Run</b><br/>road-cleaner-dashboard<br/><i>FastAPI · scales to zero</i>"]

        subgraph vertex["Vertex AI"]
            v1["Gemini 3.7 Flash<br/><i>hazard vision</i>"]
            v2["Google ADK<br/><i>jurisdiction · report prose</i>"]
            v3["Veo 3.1<br/><i>re-staging, on request</i>"]
            v4["Gemma 4<br/><i>drill scaffold</i>"]
        end

        fb["Firebase Auth<br/><i>Google sign-in → uid</i>"]

        subgraph ports["Behind ports — adapters written, not enabled on this revision"]
            fs[("Firestore")]
            gcs[("Cloud Storage")]
            ps["Pub/Sub"]
        end

        local[("SQLite + local disk<br/><i>what the deployed revision runs</i>")]
    end

    smtp["SMTP → agency desks<br/><i>guarded: allowlist + DRY_RUN</i>"]

    user -->|HTTPS| run
    user -.->|sign in| fb
    fb -.->|verified token| run
    run --> v1
    run --> v2
    run --> v3
    run --> v4
    run --> local
    run -.->|"deploy.sh --with-firestore"| ports
    run -->|"only past guard_live_send"| smtp

    classDef google fill:#1D4E6B,stroke:#0E1116,color:#EAF3F9
    classDef store fill:#F2F5F8,stroke:#94A5B4,color:#1A1A18
    classDef guard fill:#FBEDE7,stroke:#B4451F,color:#1A1A18
    class v1,v2,v3,v4,fb,run google
    class fs,gcs,local store
    class smtp guard
```

## Where the boundary sits

The one invariant worth drawing on its own: generated media and camera evidence
never mix, and nothing synthetic can reach an agency.

```mermaid
flowchart LR
    subgraph real["Camera evidence — the civic record"]
        rf["data/frames/<br/><i>/frames/ serves these</i>"]
        rc["Case.synthetic = false"]
        letter["filed report<br/>+ audit trail"]
    end

    subgraph gen["Generated — never evidence"]
        gm["data/media/synthetic/<br/><i>/media/ serves these</i>"]
        gc["Case.synthetic = true<br/><i>SIM- prefix</i>"]
        badge["badged with the model<br/>that made it"]
    end

    block{{"Dispatcher._file_locked<br/>raises SyntheticCaseError"}}

    rc --> letter
    gc --> block
    block -.->|refused| gen
    gm --- badge

    rf -.->|"never"| gen
    gm -.->|"never"| real

    classDef guard fill:#FBEDE7,stroke:#B4451F,color:#1A1A18
    class block guard
```
