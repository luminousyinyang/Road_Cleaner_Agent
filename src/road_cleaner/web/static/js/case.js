/* Case detail page.
 *
 * In the design comp "Check now" was a 1.5s setTimeout that appended a canned
 * line. Here it actually calls the Auditor: a fresh frame is pulled from the
 * camera, the vision model is asked whether the hazard from the evidence photo
 * is still there, and a real trail entry is written to the database. Reload the
 * page and it is still there, because it happened.
 */

(function () {
  "use strict";

  const CHECK_INTERVAL_SECONDS = 90;

  // --- live clock -----------------------------------------------------
  const clock = document.getElementById("clock");
  if (clock) {
    const tick = () => {
      clock.textContent = new Date().toLocaleTimeString([], {
        hour: "numeric",
        minute: "2-digit",
        second: "2-digit",
      });
    };
    tick();
    setInterval(tick, 1000);
  }

  // --- countdown to the next scheduled look ---------------------------
  const countdown = document.getElementById("countdown");
  if (countdown) {
    let remaining = CHECK_INTERVAL_SECONDS;
    const render = () => {
      const m = String(Math.floor(remaining / 60)).padStart(2, "0");
      const s = String(remaining % 60).padStart(2, "0");
      countdown.textContent = `${m}:${s}`;
      remaining = remaining > 0 ? remaining - 1 : CHECK_INTERVAL_SECONDS;
    };
    render();
    setInterval(render, 1000);
  }

  // --- "Check now" ----------------------------------------------------
  const button = document.getElementById("recheck");
  if (!button) return;

  button.addEventListener("click", async () => {
    const caseId = button.dataset.case;
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "Looking…";

    try {
      const response = await fetch(`/api/cases/${encodeURIComponent(caseId)}/recheck`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const result = await response.json();

      appendTrail(result);
      refreshFrame(result);
      button.textContent = result.still_present ? "Still there" : "Road is clear";
    } catch (err) {
      console.error("Re-check failed", err);
      button.textContent = "Couldn't reach the camera";
    } finally {
      setTimeout(() => {
        button.disabled = false;
        button.textContent = original;
      }, 2500);
    }
  });

  function appendTrail(result) {
    const trail = document.getElementById("trail");
    if (!trail || !result.trail_entry) return;

    const entry = result.trail_entry;
    const item = document.createElement("div");
    item.className = `trail__item trail__item--${entry.tone}`;

    const dot = document.createElement("span");
    dot.className = "trail__dot";

    const time = document.createElement("span");
    time.className = "trail__time";
    time.textContent = entry.time;
    const stage = document.createElement("span");
    stage.className = "trail__stage";
    stage.textContent = entry.stage;
    time.appendChild(stage);

    const text = document.createElement("span");
    text.className = "trail__text";
    // textContent, not innerHTML -- this string comes from the database.
    text.textContent = entry.text;

    item.append(dot, time, text);
    trail.appendChild(item);
    item.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function refreshFrame(result) {
    const image = document.getElementById("live-frame");
    if (!image || !result.frame_url) return;
    // Cache-bust so the freshly captured frame actually appears.
    image.src = `${result.frame_url}?t=${Date.now()}`;
  }
})();
