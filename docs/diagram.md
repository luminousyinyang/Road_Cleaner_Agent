# Architecture diagram

Mermaid rather than an exported image: it renders on GitHub, it diffs in review,
and it cannot drift out of date behind a PNG nobody regenerated.

## The system

```mermaid
flowchart TB
    subgraph sources["Public data"]
        cams["511 traffic cameras<br/>GA · FL · NC<br/><i>snapshot + incident feeds</i>"]
    end

    subgraph agents["Agent fleet — Cloud Run"]
        watcher["<b>Watcher</b><br/>polls on tiers<br/>skips identical frames"]
        analyst["<b>Analyst</b><br/>prefilter → vision → gate"]
        dispatcher["<b>Dispatcher</b><br/>jurisdiction → compose → file"]
        auditor["<b>Auditor</b><br/>re-checks until clear<br/>escalates twice, then stops"]
    end

    subgraph models["Google AI"]
        gemma["<b>Gemma 4</b><br/>gemma-4-26b-a4b-it-maas<br/><i>drill scaffold</i>"]
        gemini["<b>Gemini 3.7 Flash</b><br/>Vertex AI<br/><i>hazard vision</i>"]
        adk["<b>Google ADK</b><br/>LlmAgent + Runner<br/><i>jurisdiction, report prose</i>"]
        veo["<b>Veo 3.1</b><br/><i>dashcam re-staging</i>"]
        chirp["<b>Chirp 3 HD</b><br/><i>spoken briefing</i>"]
        lyria["<b>Lyria</b><br/><i>reel score</i>"]
    end

    subgraph gate["The confidence gate — pure Python, no model"]
        g["1 · floor 0.55<br/>2 · two frames, 90s–30min apart<br/>3 · not already in the state feed<br/>4 · severity × confidence"]
    end

    subgraph state["State"]
        repo[("Firestore / SQLite<br/>cases · detections · trail")]
        blobs[("Cloud Storage / disk<br/>evidence frames")]
        media[("media store<br/><i>generated — kept apart</i>")]
    end

    agencies["State DOT desks<br/>Open311 · form · email"]

    cams --> watcher --> analyst
    analyst -.->|frame| gemini
    analyst --> gate
    gate -->|file| dispatcher
    gate -->|watch / suppress| repo
    dispatcher -.->|whose road?| adk
    dispatcher -->|DRY_RUN=true<br/>composed, not sent| agencies
    dispatcher --> auditor
    auditor -.->|still there?| gemini
    auditor --> repo

    watcher --> blobs
    dispatcher --> repo
    repo --> veo --> media
    repo --> chirp --> media
    lyria --> media

    classDef google fill:#1D4E6B,stroke:#0E1116,color:#EAF3F9
    classDef store fill:#F2F5F8,stroke:#94A5B4,color:#1A1A18
    classDef guard fill:#FBEDE7,stroke:#B4451F,color:#1A1A18
    class gemma,gemini,adk,veo,chirp,lyria google
    class repo,blobs,media store
    class g guard
```

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
