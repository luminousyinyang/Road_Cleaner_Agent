/* Your incidents.
 *
 * A list of what you kept from the dashcam. The whole page is filled in here
 * rather than server-side, for one reason: the credential is a Firebase ID
 * token living in JavaScript, and the browser does not send it with a document
 * request. So the HTML arrives empty and public, and `GET /api/incidents` --
 * which does see the token -- decides what goes in it.
 *
 * The box is redrawn over the still from the fractions the model returned. It
 * is also burned into the stored image, so this is belt and braces; drawing it
 * live means the caption stays legible at whatever size the card renders.
 */

(function () {
  "use strict";

  const root = document.getElementById("inc-list");
  if (!root) return;

  const gate = document.getElementById("inc-gate");
  const loading = document.getElementById("inc-loading");
  const empty = document.getElementById("inc-empty");
  const errorSlot = document.getElementById("inc-error");
  const auth = window.RoadCleaner?.auth;

  document.querySelector("[data-gate-signin]")?.addEventListener("click", () => {
    auth?.signIn().catch((err) => {
      if (err?.code !== "auth/popup-closed-by-user") fail(err?.message || String(err));
    });
  });

  if (!auth) {
    fail("Sign-in did not load, so there is no way to fetch your incidents.");
    return;
  }

  auth.onChange((user) => {
    if (!user) {
      show({ gate: true });
      root.replaceChildren();
      return;
    }
    load();
  });

  function show(state) {
    if (gate) gate.hidden = !state.gate;
    if (loading) loading.hidden = !state.loading;
    if (empty) empty.hidden = !state.empty;
    root.hidden = !state.list;
    if (errorSlot && !state.error) errorSlot.hidden = true;
  }

  function fail(message) {
    if (!errorSlot) return;
    errorSlot.textContent = message;
    errorSlot.hidden = false;
    if (loading) loading.hidden = true;
  }

  async function load() {
    show({ loading: true });
    try {
      const response = await auth.fetch("/api/incidents");
      if (!response.ok) {
        fail(await describe(response));
        return;
      }
      const { incidents } = await response.json();
      if (!incidents.length) {
        show({ empty: true });
        return;
      }
      root.replaceChildren(...incidents.map(card));
      show({ list: true });
    } catch (err) {
      fail(`Could not load your incidents: ${(err && err.message) || err}`);
    }
  }

  function card(incident) {
    const article = document.createElement("article");
    article.className = "inc__card";

    if (incident.image_url) {
      const figure = document.createElement("div");
      figure.className = "inc__media";

      const img = document.createElement("img");
      img.src = incident.image_url;
      img.alt = `${incident.hazard} at ${incident.location}`;
      img.loading = "lazy";
      figure.appendChild(img);

      if (incident.box) {
        const box = document.createElement("div");
        box.className = incident.box_measured ? "inc__box" : "inc__box inc__box--soft";
        box.style.left = pct(incident.box.x);
        box.style.top = pct(incident.box.y);
        box.style.width = pct(incident.box.width);
        box.style.height = pct(incident.box.height);
        if (incident.box_label) {
          const label = document.createElement("span");
          label.className = "inc__box-label";
          label.textContent = incident.box_label;
          box.appendChild(label);
        }
        figure.appendChild(box);
      }
      article.appendChild(figure);
    }

    const heading = document.createElement("h2");
    heading.className = "inc__hazard";
    heading.textContent = `${incident.hazard} · ${incident.confidence.toFixed(2)}`;
    article.appendChild(heading);

    if (incident.description) {
      article.appendChild(line("inc__desc", incident.description));
    }

    const facts = document.createElement("dl");
    facts.className = "inc__facts";
    fact(facts, "Where", incident.location || "—");
    fact(facts, "When", incident.when);
    fact(facts, "Agency", incident.agency || "none resolved");
    fact(
      facts,
      "Emailed to",
      incident.emailed_to || "not sent — the mail server refused"
    );
    // Three states, because "we did not try" and "we tried and were refused"
    // are different facts, and the second usually means DASHCAM_NOTIFY_DOT is on
    // while the address is not allowlisted. That is a settings answer, so say it.
    fact(facts, "DOT", dotLine(incident));
    article.appendChild(facts);

    const details = document.createElement("details");
    details.className = "inc__report";
    const summary = document.createElement("summary");
    summary.textContent = "The report";
    details.appendChild(summary);
    const body = document.createElement("pre");
    body.className = "inc__body";
    body.textContent = incident.body;
    details.appendChild(body);
    article.appendChild(details);

    return article;
  }

  function dotLine(incident) {
    if (incident.dot_state === "sent") return `sent to ${incident.dot_destination}`;
    if (incident.dot_state === "refused") return `not sent — ${incident.dot_error}`;
    return "not sent — reporting to agencies is off";
  }

  function fact(list, label, value) {
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value;
    list.append(dt, dd);
  }

  function line(className, text) {
    const p = document.createElement("p");
    p.className = className;
    p.textContent = text;
    return p;
  }

  function pct(value) {
    return `${(value * 100).toFixed(2)}%`;
  }

  async function describe(response) {
    try {
      const data = await response.json();
      return data.detail || `That failed (${response.status}).`;
    } catch {
      return `That failed (${response.status}).`;
    }
  }
})();
