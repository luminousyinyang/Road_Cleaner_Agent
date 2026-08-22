/* Generating a dashcam clip from the case page.
 *
 * A Veo render takes about a minute, so the button starts a background job and
 * this polls it until a clip exists.
 *
 * The progress bar is an estimate and says so. Veo reports no percentage --
 * an operation is running or it is done -- so the bar is elapsed time against a
 * typical render, and it deliberately stops short of full while still running.
 * Animating a made-up number to 99% would look more polished and tell the user
 * something untrue about how close they are.
 */

(function () {
  "use strict";

  const root = document.getElementById("gen");
  if (!root) return;

  const button = document.getElementById("gen-run");
  const progress = document.getElementById("gen-progress");
  const fill = document.getElementById("gen-fill");
  const status = document.getElementById("gen-status");
  const output = document.getElementById("gen-output");
  const caseId = root.dataset.case;

  const POLL_MS = 2000;
  let timer = null;

  if (button && !button.disabled) {
    button.addEventListener("click", start);
  }

  async function start() {
    button.disabled = true;
    progress.hidden = false;
    setBar(0, "Starting render…");

    let response;
    try {
      response = await fetch(`/api/simulate/${encodeURIComponent(caseId)}`, {
        method: "POST",
      });
    } catch (err) {
      return fail("Could not reach the server.");
    }

    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      // The server explains refusals properly -- generation disabled, hazard
      // that we decline to simulate -- so show what it said.
      return fail(body.detail || `Request failed (${response.status}).`);
    }
    poll(body.id);
  }

  function poll(jobId) {
    clearInterval(timer);
    timer = setInterval(async () => {
      let job;
      try {
        const r = await fetch(`/api/simulate/jobs/${jobId}`);
        if (!r.ok) throw new Error();
        job = await r.json();
      } catch (err) {
        clearInterval(timer);
        return fail("Lost track of the render job.");
      }

      if (job.state === "running") {
        setBar(
          job.estimated_fraction,
          `Rendering… ${Math.round(job.elapsed)}s elapsed ` +
            `(usually about ${Math.round(job.typical_seconds)}s)`
        );
        return;
      }

      clearInterval(timer);
      if (job.state === "failed") return fail(job.error || "Render failed.");
      succeed(job);
    }, POLL_MS);
  }

  function succeed(job) {
    setBar(1, `Done in ${Math.round(job.elapsed)}s.`);
    button.disabled = false;

    const figure = document.createElement("figure");
    figure.className = "synth__item";

    const video = document.createElement("video");
    video.controls = true;
    video.preload = "metadata";
    video.playsInline = true;
    video.width = 640;
    video.height = 360;
    video.src = job.clip_url;

    const caption = document.createElement("figcaption");
    caption.className = "synth__badge";
    // The badge is not decoration. It comes from the clip's own provenance
    // record, and generated media is never shown without it.
    caption.textContent = job.clip_badge || "SYNTHETIC — generated";

    figure.append(video, caption);
    output.prepend(figure);
    figure.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function fail(message) {
    clearInterval(timer);
    button.disabled = false;
    progress.hidden = false;
    fill.style.width = "0%";
    status.textContent = message;
    status.classList.add("is-error");
  }

  function setBar(fraction, message) {
    fill.style.width = `${Math.round(fraction * 100)}%`;
    status.textContent = message;
    status.classList.remove("is-error");
  }
})();
