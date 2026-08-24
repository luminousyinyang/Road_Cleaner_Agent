/* Generating a dashcam clip from the scenario library.
 *
 * A Veo render takes about two minutes, so each button starts a background job
 * and this polls it until a clip exists. Several cards can render at once, so
 * every control is wired independently rather than through page-level globals.
 *
 * The progress bar is an estimate and says so. Veo reports no percentage -- an
 * operation is running or it is done -- so the bar is elapsed time against a
 * typical render, and it deliberately stops short of full while still running.
 * Animating a made-up number to 99% would look more polished and tell the user
 * something untrue about how close they are.
 */

(function () {
  "use strict";

  const POLL_MS = 2000;

  document.querySelectorAll(".gen").forEach(setup);

  function setup(root) {
    const button = root.querySelector(".gen-run");
    const progress = root.querySelector(".gen__progress");
    const fill = root.querySelector(".gen__bar span");
    const status = root.querySelector(".gen__status");
    const caseId = root.dataset.case;
    if (!button || button.disabled) return;

    let timer = null;

    button.addEventListener("click", start);

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
        // The server explains refusals properly -- generation disabled, or a
        // hazard we decline to simulate -- so show what it said.
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

      const card = root.closest(".card");
      if (!card) return;

      // Swap the empty placeholder for the clip, badge first. Generated media is
      // never inserted without the badge that identifies the model behind it.
      const media = card.querySelector(".card__media");
      if (media) {
        media.classList.remove("card__media--empty");
        media.replaceChildren();

        const badge = document.createElement("span");
        badge.className = "chip-syn";
        badge.textContent = job.clip_badge_short || job.clip_badge || "SYNTHETIC — generated";

        const video = document.createElement("video");
        video.controls = true;
        video.preload = "metadata";
        video.playsInline = true;
        video.src = `${job.clip_url}#t=0.5`;

        media.append(badge, video);
      }
      card.classList.remove("card--generate");
      card.classList.add("card--clip");
      root.remove();
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
  }
})();
