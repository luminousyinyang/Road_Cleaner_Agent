/* Running the agent over a clip, watched.
 *
 * The button starts a job; this polls it and paints whatever has arrived. The
 * server publishes after every frame rather than every stage, so boxes appear
 * one at a time on the video as the model returns them, which is the whole
 * point -- a spinner that resolves into a finished answer would prove nothing
 * that the old prose version did not.
 *
 * Two rules this file exists to keep:
 *
 * 1. Nothing here invents anything. Every number, box, sentence and verdict
 *    comes from the payload. Where the server says a result was replayed from
 *    cache, that is shown rather than smoothed over.
 * 2. Send does not send. It composes and stops, and says why. The restraint is
 *    the feature, so it is a real button that gives a real answer -- not a
 *    disabled control that reads as unfinished.
 */

(function () {
  "use strict";

  const root = document.getElementById("run");
  if (!root) return;

  const caseId = root.dataset.case;
  const video = document.getElementById("run-video");
  const boxes = document.getElementById("run-boxes");
  const stages = document.getElementById("run-stages");
  const frameList = document.getElementById("run-frames");
  const emptyRow = document.getElementById("run-frames-empty");
  const startButton = document.getElementById("run-start");
  const status = document.getElementById("run-status");
  const note = document.getElementById("run-note");
  const errorSlot = document.getElementById("run-error");
  const report = document.getElementById("run-report");
  const evidence = document.getElementById("run-evidence");

  // How often to ask. The run makes one model call per frame and each takes a
  // second or two, so a tighter loop would mostly poll a job that has not moved.
  const POLL_MS = 700;

  // How close the playhead has to be to a sampled moment for that moment's box
  // to be the truth on screen. Samples sit ~1.8s apart in an 8s clip, so half of
  // that gives each frame its own stretch of footage and no overlap.
  const NEAR_SECONDS = 0.9;

  // The last result painted, so the Send button knows where this would go and
  // the playhead knows which boxes exist. Set by `paint`, read by everything.
  let current = null;
  // True only while a run is streaming. During a run the newest box wins,
  // because watching them land is the demo; afterwards the playhead decides.
  let running = false;

  // Repaint from whatever the last run left behind, so the page is not blank
  // before anyone clicks.
  if (root.dataset.analysis) {
    try {
      paint(JSON.parse(root.dataset.analysis));
      syncToPlayhead();
    } catch (err) {
      console.warn("Could not read the cached analysis", err);
    }
  }

  startButton.addEventListener("click", start);

  async function start() {
    startButton.disabled = true;
    startButton.textContent = "Running…";
    errorSlot.hidden = true;
    running = true;
    clearFrames();

    try {
      const response = await fetch(`/api/cases/${encodeURIComponent(caseId)}/inspect`, {
        method: "POST",
      });
      if (!response.ok) throw new Error(await describe(response));
      await poll((await response.json()).id);
    } catch (err) {
      fail(err.message || "Could not start the analysis.");
    } finally {
      // The run is over, so the playhead takes the boxes back.
      running = false;
      syncToPlayhead();
      startButton.disabled = false;
      startButton.textContent = "Run it again";
    }
  }

  async function poll(jobId) {
    for (;;) {
      const response = await fetch(`/api/inspect/${encodeURIComponent(jobId)}`);
      if (!response.ok) throw new Error(await describe(response));
      const job = await response.json();

      if (job.result) paint(job.result);
      if (job.state === "failed") {
        fail(job.error || "The analysis failed.");
        return;
      }
      if (job.state === "done") {
        status.textContent = summarise(job.result);
        return;
      }
      await wait(POLL_MS);
    }
  }

  // --- painting -------------------------------------------------------

  function paint(result) {
    current = result;
    paintStages(result.stages || []);
    paintFrames(result.frames || []);
    paintReport(result);
    paintEvidence(result);

    if (result.from_cache && result.cache_note) {
      note.textContent = result.cache_note;
      note.hidden = false;
    } else {
      note.hidden = true;
    }
  }

  function paintStages(list) {
    list.forEach((stage) => {
      const row = stages.querySelector(`[data-stage="${stage.key}"]`);
      if (!row) return;
      row.className = `stage is-${stage.state}`;
      row.querySelector(".stage__detail").textContent = stage.detail || "";
    });
  }

  function paintFrames(rows) {
    if (rows.length && emptyRow) emptyRow.hidden = true;

    rows.forEach((row) => {
      let item = frameList.querySelector(`[data-frame="${row.index}"]`);
      if (!item) {
        item = document.createElement("li");
        item.dataset.frame = String(row.index);
        item.className = "look run__frame";
        // Clicking a result seeks the video to the moment it came from, which
        // is the only way to check the box by eye against the footage.
        item.addEventListener("click", () => seek(row));
        frameList.appendChild(item);
      }

      item.classList.toggle("run__frame--found", row.state === "found");
      item.classList.toggle("run__frame--clear", row.state === "clear");
      item.replaceChildren(
        span("look__time", row.stamp || ""),
        span("look__label", describeFrame(row)),
        span("run__conf", row.confidence != null ? row.confidence.toFixed(2) : "")
      );

      if (row.state === "found" && row.box) drawBox(row);
    });
  }

  function describeFrame(row) {
    if (row.state === "looking") return "looking…";
    if (row.state === "clear") return "nothing here";
    // Deliberately not `row.lane`. It was the only raw token shown unformatted,
    // it was frequently wrong, and "unknown" is truthy so it rendered as
    // "debris · unknown". The box says where the thing is.
    let text = row.hazard_label || row.hazard || "";
    // An approximated box is not a measurement, and the page must not present
    // it as one. `box_is_measured` is carried all the way from the detection
    // for exactly this line.
    if (row.box && !row.box_measured) text += " · box approximate";
    return text;
  }

  function drawBox(row) {
    let box = boxes.querySelector(`[data-box="${row.index}"]`);
    if (!box) {
      box = document.createElement("div");
      box.dataset.box = String(row.index);
      box.className = "box";
      const label = document.createElement("span");
      label.className = "box__label";
      box.appendChild(label);
      boxes.appendChild(box);
    }
    box.style.left = pct(row.box.x);
    box.style.top = pct(row.box.y);
    box.style.width = pct(row.box.width);
    box.style.height = pct(row.box.height);
    box.classList.toggle("box--soft", !row.box_measured);
    box.querySelector(".box__label").textContent = row.box_label || "";
    // Only while streaming. Repainting a finished run -- on page load, from the
    // cached analysis -- must not leave the last box hanging over the footage;
    // the playhead decides then.
    if (running) showOnly(row.index);
  }

  /* One box at a time. Five boxes at once looks like five hazards, when it is
     one object seen five times as the car closes on it.

     `index` of null hides every box, which is the state the video spends most of
     its time in. */
  function showOnly(index) {
    boxes.querySelectorAll(".box").forEach((box) => {
      box.hidden = index === null || box.dataset.box !== String(index);
    });
  }

  /* A box belongs to a moment, not to the page.

     It used to be drawn and then left there: the last box of a run sat over the
     footage for as long as the page was open, hovering above a road that had
     moved on. Now the playhead decides -- the box for the moment being shown, or
     no box at all. */
  function boxForTime(seconds) {
    const rows = (current && current.frames) || [];
    let best = null;
    rows.forEach((row) => {
      if (row.state !== "found" || !row.box || row.at == null) return;
      const gap = Math.abs(row.at - seconds);
      if (gap <= NEAR_SECONDS && (best === null || gap < best.gap)) {
        best = { gap, index: row.index };
      }
    });
    return best ? best.index : null;
  }

  function syncToPlayhead() {
    if (running || !video) return;
    const index = boxForTime(video.currentTime);
    showOnly(index);
    frameList.querySelectorAll(".run__frame").forEach((item) => {
      item.classList.toggle("run__frame--at", item.dataset.frame === String(index));
    });
  }

  if (video) {
    video.addEventListener("timeupdate", syncToPlayhead);
    video.addEventListener("seeked", syncToPlayhead);
    video.addEventListener("loadedmetadata", syncToPlayhead);
  }

  function seek(row) {
    if (!video || row.at == null) return;
    video.currentTime = row.at;
    video.pause();
    showOnly(row.index);
    frameList.querySelectorAll(".run__frame").forEach((item) => {
      item.classList.toggle("run__frame--at", item.dataset.frame === String(row.index));
    });
  }

  function paintReport(result) {
    if (!result.report_body) {
      report.hidden = true;
      return;
    }
    document.getElementById("run-subject").textContent = result.report_subject || "";
    document.getElementById("run-body").textContent = result.report_body;

    const send = document.getElementById("run-send");
    send.dataset.agency = result.agency || "";
    send.textContent = `Send to ${result.agency || "the agency"}`;
    send.disabled = false;
    report.hidden = false;
  }

  function paintEvidence(result) {
    if (!result.evidence_url) {
      evidence.hidden = true;
      return;
    }
    const image = document.getElementById("run-evidence-img");
    // Cache-bust: the key is stable across runs, so the browser would keep
    // showing the previous run's still.
    image.src = `${result.evidence_url}?t=${Date.now()}`;
    document.getElementById("run-evidence-at").textContent =
      result.evidence_at != null ? `From ${result.evidence_at.toFixed(1)}s into the clip.` : "";
    evidence.hidden = false;
  }

  function summarise(result) {
    if (!result) return "Done.";
    const found = (result.frames || []).filter((f) => f.state === "found").length;
    const total = (result.frames || []).length;
    const model = result.model_name || "the model";
    const cached = result.from_cache ? " · replayed from cache" : "";
    return `${found} of ${total} frames found something · ${model}${cached}`;
  }

  // --- send, which does not send --------------------------------------

  /* Send reveals the request. It does not go anywhere.

     The first version opened the agency's intake page in a tab. Every address
     in the seed registry is `example.invalid` by design, so that navigated
     people off the demo and onto a DNS error -- and even against a real
     endpoint, throwing a viewer at a blank third-party form loses the one thing
     worth showing: the filled-in request. So the panel shows what would be
     transmitted, and the link is offered for whoever wants to take it. */
  document.getElementById("run-send")?.addEventListener("click", (event) => {
    const button = event.currentTarget;
    const panel = document.getElementById("run-handover");
    const destination = (current && current.report_destination) || "";
    const channel = (current && current.report_channel) || "";
    const payload = (current && current.report_payload) || {};
    const agency = (current && current.agency) || "the agency";

    document.getElementById("run-sent").textContent = destination
      ? `Composed and stopped. Nothing was transmitted to ${agency}.`
      : `No agency resolved, so there is nowhere to send this. Held rather than misfiled.`;
    document.getElementById("run-to").textContent =
      (current && current.report_email) || destination || "—";
    document.getElementById("run-by").textContent = describeChannel(channel);

    paintFields(payload);
    offerLink(destination, channel);

    panel.hidden = false;
    button.disabled = true;
    button.textContent = "Composed — not sent";
  });

  function describeChannel(channel) {
    if (channel === "email") return "email to the district maintenance desk";
    if (channel === "open311") return "Open311 service request (HTTP POST)";
    if (channel === "maintenance_form") return "the agency's maintenance request form (HTTP POST)";
    return channel || "—";
  }

  /* The fields the channel would actually submit. The body is already on screen
     above, so it is summarised rather than repeated a third time. */
  function paintFields(payload) {
    const list = document.getElementById("run-fields");
    const label = document.getElementById("run-fields-label");
    list.replaceChildren();

    const names = Object.keys(payload);
    label.hidden = names.length === 0;
    names.forEach((name) => {
      const value = String(payload[name] ?? "");
      const term = document.createElement("dt");
      term.textContent = name;
      const detail = document.createElement("dd");
      detail.textContent =
        value.length > 90 ? `${value.slice(0, 90)}… (${value.length} characters)` : value || "—";
      list.append(term, detail);
    });
  }

  /* Send opens a mail draft. Always.

     It used to open the agency's intake page in a tab, which meant leaving the
     demo for a third-party form -- and for the placeholder addresses this
     shipped with, a DNS error. A draft is better on every count: the report is
     already written, it stays on this machine until somebody presses send, and
     it works the same whether or not that DOT publishes an inbox.

     Most do not. They route reports through a web form on purpose, so the
     ticket lands in their system rather than a mailbox. Where that is the case
     the draft still opens with the report in it, and carries the form's URL so
     it can be pasted in. Nothing is invented to fill the To: line. */
  function offerLink(destination, channel) {
    const link = document.getElementById("run-open");
    const email = (current && current.report_email) || "";
    const subject = (current && current.report_subject) || "";
    let body = (current && current.report_body) || "";

    if (!email && destination) {
      body += `\n\n${(current && current.agency) || "This agency"} does not publish `
        + `an email for this. Submit it at:\n${destination}`;
    }

    link.hidden = false;
    link.removeAttribute("target");
    link.href =
      `mailto:${encodeURIComponent(email)}` +
      `?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
    link.textContent = email ? `Open a draft to ${email}` : "Open a draft";
  }

  document.getElementById("run-copy")?.addEventListener("click", async (event) => {
    const said = document.getElementById("run-copied");
    const ok = await copy((current && current.report_body) || "");
    said.textContent = ok ? "Copied." : "Could not reach the clipboard — select it above.";
    said.hidden = false;
    event.currentTarget.blur();
  });

  async function copy(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // Insecure context, or permission refused. Not worth failing over.
      return false;
    }
  }

  // --- odds and ends --------------------------------------------------

  function clearFrames() {
    frameList.querySelectorAll(".run__frame").forEach((item) => item.remove());
    boxes.replaceChildren();
    if (emptyRow) emptyRow.hidden = false;
    stages.querySelectorAll(".stage").forEach((row) => {
      row.className = "stage";
      row.querySelector(".stage__detail").textContent = "";
    });
  }

  function fail(message) {
    errorSlot.textContent = message;
    errorSlot.hidden = false;
    status.textContent = "Stopped.";
  }

  async function describe(response) {
    try {
      const body = await response.json();
      return body.detail || `HTTP ${response.status}`;
    } catch {
      return `HTTP ${response.status}`;
    }
  }

  function span(className, text) {
    const element = document.createElement("span");
    element.className = className;
    // textContent throughout: every string here came off the network.
    element.textContent = text;
    return element;
  }

  const pct = (fraction) => `${(fraction * 100).toFixed(1)}%`;
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
})();
