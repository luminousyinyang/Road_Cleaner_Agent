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
 *
 * The stills are fetched, not linked, and that is the same problem again rather
 * than a preference. `<img src="/api/incidents/…/image">` makes a plain browser
 * request, and a browser does not put an Authorization header on one -- so the
 * route, which quite rightly demands a verified uid, answered every one of them
 * with 401 and every card showed a broken-image icon. The bytes have to come
 * back through `auth.fetch` like everything else here, and go into the tag as a
 * blob URL. See `paintImage`.
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
      img.alt = `${incident.hazard} at ${incident.location}`;
      // No `src` yet, and no `loading="lazy"` either -- neither means anything
      // for an image whose bytes arrive over fetch. `paintImage` fills it in.
      paintImage(img, incident);
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

    // Only when somebody else reported it too. A badge reading "1 report" on
    // every card is decoration; the number is worth showing precisely when it
    // is greater than one, because that is when it changed what we did.
    if (incident.reports_24h > 1) {
      const badge = document.createElement("p");
      badge.className = "inc__tally";
      const count = document.createElement("strong");
      count.textContent = String(incident.reports_24h);
      badge.append(
        count,
        ` reports of this in ${incident.dedup_window_hours}h`
      );
      if (incident.dedup_reason) {
        badge.append(" · mail held");
      }
      article.appendChild(badge);
    }

    if (incident.description) {
      article.appendChild(line("inc__desc", incident.description));
    }

    const facts = document.createElement("dl");
    facts.className = "inc__facts";
    fact(facts, "Where", incident.location || "—");
    fact(facts, "When", incident.when);
    fact(facts, "Agency", incident.agency || "none resolved");
    fact(facts, "Emailed to", mailLine(incident));
    // No DOT row. It spent most of its life reading "not sent — reporting to
    // agencies is off", which is a fact about this deployment's configuration
    // rather than about the hazard in the picture, and it was the same on every
    // card. `dot_state` is still on the API for anyone who wants it.
    article.appendChild(facts);
    // Below the facts, because it is the explanation for the two "not sent"
    // lines directly above it rather than a fact of its own.
    if (incident.dedup_reason) {
      article.appendChild(line("inc__held", incident.dedup_reason));
    }

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

  function mailLine(incident) {
    if (incident.emailed_to) return incident.emailed_to;
    // A held duplicate never reached the mail server, so blaming the mail
    // server for it would be a plain untruth on the card.
    if (incident.dedup_reason) return "not sent — already reported";
    return "not sent — the mail server refused";
  }

  /* Fetch the still with the bearer token and put it in the tag.

     The route resolves the blob key server-side from a record looked up under
     the caller's own uid, which is what makes it safe -- and also what makes it
     impossible to load with a bare `src`, because the browser sends no token on
     an image request. So the bytes come back through `auth.fetch` and go in as a
     blob URL.

     Failure is left as an empty `<img>` rather than an error on the card. The
     record is the thing worth keeping and the rest of it is right there; a whole
     card turning red because one photograph would not load overstates it. */
  async function paintImage(img, incident) {
    try {
      const response = await auth.fetch(incident.image_url);
      if (!response.ok) return;
      const blob = await response.blob();
      img.src = URL.createObjectURL(blob);
      // Released once decoded. Without this every card holds its still in memory
      // for the life of the tab, which on a phone is a real cost after a few
      // dozen of them.
      img.addEventListener("load", () => URL.revokeObjectURL(img.src), { once: true });
    } catch {
      /* leave it blank */
    }
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
