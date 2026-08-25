/* The drill console.
 *
 * Posts a hazard description, then polls the job and repaints the six stages as
 * they complete. The point of the polling is that the stages are visible while
 * they happen -- a spinner that resolves into a finished report would hide the
 * only interesting thing about this, which is that a real pipeline is running.
 */

(function () {
  "use strict";

  /* Where the drill happens. Null until somebody drops a pin, and the server
     treats that as "invent a location" exactly as it always did -- the map adds
     an option rather than adding a required field. */
  let picked = null;

  window.addEventListener("load", () => {
    const element = document.getElementById("drill-map");
    if (!element || !window.RoadCleanerMap) return;
    window.RoadCleanerMap.attach(element, {
      status: document.getElementById("drill-where"),
      onPick: (found) => {
        picked = found ? { lat: found.lat, lng: found.lng } : null;
      },
    });
  });

  const root = document.getElementById("drill");
  if (!root) return;

  const form = document.getElementById("drill-form");
  const input = document.getElementById("drill-prompt");
  const button = document.getElementById("drill-run");
  const stagesEl = document.getElementById("drill-stages");
  const errorEl = document.getElementById("drill-error");
  const outEl = document.getElementById("drill-out");
  const framesEl = document.getElementById("drill-frames");

  const POLL_MS = 900;
  let timer = null;

  document.querySelectorAll(".drill__example").forEach((chip) => {
    chip.addEventListener("click", () => {
      input.value = chip.textContent.trim();
      input.focus();
    });
  });

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    start();
  });

  async function start() {
    button.disabled = true;
    button.textContent = "Running…";
    errorEl.hidden = true;
    outEl.hidden = true;
    framesEl.replaceChildren();
    stagesEl.hidden = false;
    resetStages();

    let response;
    try {
      response = await fetch("/api/drill", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(
          picked
            ? { prompt: input.value, lat: picked.lat, lng: picked.lng }
            : { prompt: input.value }
        ),
      });
    } catch (err) {
      return fail("Could not reach the server.");
    }

    const body = await response.json().catch(() => ({}));
    if (!response.ok) return fail(body.detail || `Request failed (${response.status}).`);
    poll(body.id);
  }

  function poll(jobId) {
    clearInterval(timer);
    // A poll routed to another instance is told there is no such drill, about a
    // drill that is still running where it started. Tolerated a few times before
    // it is treated as lost; see inspect.js for the full reasoning.
    let misses = 0;
    const MISSES_ALLOWED = 5;
    timer = setInterval(async () => {
      let job;
      try {
        const r = await fetch(`/api/drill/${jobId}`);
        if (r.status === 404 && misses < MISSES_ALLOWED) {
          misses += 1;
          return;
        }
        if (!r.ok) throw new Error();
        misses = 0;
        job = await r.json();
      } catch (err) {
        return fail("Lost track of the drill.");
      }

      if (job.result) paint(job.result);

      if (job.state === "running") return;
      clearInterval(timer);
      button.disabled = false;
      button.textContent = "Run the drill";
      if (job.state === "failed") return fail(job.error || "The drill failed.");
      finish(job.result);
    }, POLL_MS);
  }

  function resetStages() {
    stagesEl.querySelectorAll(".stage").forEach((li) => {
      li.className = "stage";
      li.querySelector(".stage__detail").textContent = "";
    });
  }

  function paint(result) {
    (result.stages || []).forEach((stage) => {
      const li = stagesEl.querySelector(`[data-stage="${stage.key}"]`);
      if (!li) return;
      li.className = `stage is-${stage.state}`;
      li.querySelector(".stage__detail").textContent = stage.detail || "";
    });

    // The staged frames are worth showing as soon as they exist: they are what
    // the vision model is about to be asked about.
    if (result.frame_urls && result.frame_urls.length && !framesEl.childElementCount) {
      result.frame_urls.forEach((url, i) => {
        const figure = document.createElement("figure");
        figure.className = "drill__frame";
        const img = document.createElement("img");
        img.src = url;
        img.alt = i === 0 ? "First sighting" : "Confirmation";
        img.loading = "lazy";
        const cap = document.createElement("figcaption");
        cap.textContent = i === 0 ? "First sighting" : "Confirmation, 4 min later";
        figure.append(img, cap);
        framesEl.appendChild(figure);
      });
    }
  }

  function finish(result) {
    if (!result) return;
    outEl.hidden = false;

    text("drill-case", result.case_id || "");
    text("drill-subject", result.report_subject || "");
    text("drill-body", result.report_body || "");
    text("drill-agency", result.agency || "the agency");

    // Belt and braces. The server never returns filed=true -- but if it somehow
    // did, the page should say so rather than keep claiming nothing was sent.
    const send = document.getElementById("drill-send");
    if (result.filed) {
      send.textContent = "Filed";
      send.disabled = true;
      send.classList.add("is-wrong");
    }
    outEl.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function text(id, value) {
    const el = document.getElementById(id);
    // textContent, not innerHTML -- this is model output.
    if (el) el.textContent = value;
  }

  function fail(message) {
    clearInterval(timer);
    button.disabled = false;
    button.textContent = "Run the drill";
    errorEl.textContent = message;
    errorEl.hidden = false;
  }
})();
