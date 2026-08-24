/* Case detail page.
 *
 * In the design comp "Check now" was a 1.5s setTimeout that appended a canned
 * line. Here it actually calls the Auditor: a fresh frame is pulled from the
 * camera, the vision model is asked whether the hazard from the evidence photo
 * is still there, and a real trail entry is written to the database. Reload the
 * page and it is still there, because it happened.
 */

/* Two things used to live here and no longer do:
 *
 * A wall clock, which showed the *browser's* current time in the header of a
 * case last updated weeks ago, and read its own `data-since` attribute nowhere.
 *
 * A countdown to the "next look", counting down from a hardcoded 90 seconds,
 * wired to nothing, looping forever -- and still promising another look on cases
 * that had been closed for days. Both were motion rather than information. The
 * SLA note now sits in that space and is computed from the case.
 */

(function () {
  "use strict";

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
      // The trail entry this writes lands about a thousand pixels further down
      // the page, so from the button the whole thing looked like it did nothing.
      // Say what happened where the click happened.
      announce(result);
      if (result.ran === false) {
        button.textContent = "Nothing to check";
      } else {
        button.textContent = result.still_present ? "Still there" : "Road is clear";
      }
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

  function announce(result) {
    const slot = document.getElementById("recheck-said");
    if (!slot) return;
    // The server always sends a `message`. Relying on `trail_entry` here was the
    // bug: the Auditor can look and find nothing worth writing down, and on a
    // closed case it does not run at all -- both produced an empty line, which
    // read as the button being broken.
    slot.textContent = result.message || "Checked.";
    slot.hidden = false;
  }

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

/* Where this case is, and moving it.
 *
 * A case's location was fixed when it opened, from whatever camera saw it. That
 * is right for a camera and wrong for a re-staged clip, which could have
 * happened anywhere -- so the pin is draggable and the agency follows it.
 *
 * Deliberately two steps. Clicking the map only proposes; a second, named click
 * commits. Moving a case rewrites a stored record, and a stray click on a map
 * should not do that.
 */
(function () {
  "use strict";

  const element = document.getElementById("case-map");
  if (!element) return;

  const status = document.getElementById("case-where");
  const button = document.getElementById("case-move");
  const said = document.getElementById("case-moved");
  const caseId = document.getElementById("run")?.dataset.case
    || document.getElementById("recheck")?.dataset.case;

  let proposed = null;

  window.addEventListener("load", () => {
    if (!window.RoadCleanerMap) return;
    const lat = parseFloat(element.dataset.lat);
    const lng = parseFloat(element.dataset.lng);

    window.RoadCleanerMap.attach(element, {
      lat: Number.isFinite(lat) ? lat : undefined,
      lng: Number.isFinite(lng) ? lng : undefined,
      status,
      onPick: (found) => {
        proposed = found ? { lat: found.lat, lng: found.lng } : null;
        button.hidden = !proposed;
        said.hidden = true;
      },
    });
  });

  button?.addEventListener("click", async () => {
    if (!proposed || !caseId) return;
    button.disabled = true;
    try {
      const response = await fetch(
        `/api/cases/${encodeURIComponent(caseId)}/location`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(proposed),
        }
      );
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);

      said.textContent = `Moved. Now ${body.location}, and ${body.agency || "nobody"} owns it.`;
      said.hidden = false;
      button.hidden = true;
      // The header, the report and the form payload all read from the case, so
      // they are stale the moment this succeeds. Reload rather than patch six
      // places and risk one of them disagreeing.
      setTimeout(() => window.location.reload(), 1200);
    } catch (err) {
      said.textContent = `Could not move it: ${(err && err.message) || err}`;
      said.hidden = false;
      button.disabled = false;
    }
  });
})();
