/* The agent, pointed at a real road.
 *
 * Everywhere else on this site the model reads a generated clip or a public
 * traffic camera. Here it reads whatever is in front of your phone, and the
 * boxes land on real objects on a real road. Same prompt, same model, same
 * drawing code -- the only thing that changed is where the pixels come from.
 *
 * Three constraints shape all of it:
 *
 * 1. Quota. Each look is a Vertex call and there are only twenty or thirty of
 *    them before it throttles. So: one request in flight at a time, a deliberate
 *    gap between looks, and a counter on screen. A demo that dies mysteriously
 *    at look twenty-two is worse than one that tells you it is at look nineteen.
 * 2. Looking stops the moment something is found. A hazard is one object, and
 *    spending more quota on the thing you have already seen is waste. The find
 *    is held for a fixed window -- see `hold` -- and then dropped.
 * 3. **A frame is only kept if you press the button.** Every frame goes to the
 *    model and is discarded. The one exception is a find you actively report:
 *    that still is stored and mailed to you, and it is the only thing here that
 *    ever leaves the browser to be written down. A find that times out unpressed
 *    leaves nothing behind at all -- no record, no mail, nothing uploaded.
 */

(function () {
  "use strict";

  const root = document.getElementById("dash");
  if (!root) return;

  const video = document.getElementById("dash-video");
  const boxes = document.getElementById("dash-boxes");
  const idle = document.getElementById("dash-idle");
  const toggle = document.getElementById("dash-toggle");
  const status = document.getElementById("dash-status");
  const counter = document.getElementById("dash-count");
  const said = document.getElementById("dash-said");
  const whereSlot = document.getElementById("dash-where");
  const errorSlot = document.getElementById("dash-error");
  const reportButton = document.getElementById("dash-report");
  const gate = document.getElementById("dash-gate");
  const shareButton = document.getElementById("dash-share");

  const auth = window.RoadCleaner?.auth;
  const authConfigured = root.dataset.auth === "on";

  // Long enough that a minute of looking costs about twenty calls rather than
  // sixty, short enough that the boxes still feel attached to the road.
  const EVERY_MS = 3000;

  // How long a find stays reportable before it is dropped. Server-supplied
  // (DASHCAM_REPORT_WINDOW_SECONDS) so the countdown on screen and the number in
  // the page copy cannot disagree with each other.
  const HOLD_MS = (Number(root.dataset.reportWindow) || 15) * 1000;

  // Wide enough for the model to see a tyre tread a hundred metres off, small
  // enough that the upload is a fraction of a second on a phone. Box coordinates
  // come back as fractions, so shrinking the wire image costs nothing on screen.
  const SEND_WIDTH = 960;
  const JPEG_QUALITY = 0.8;

  let stream = null;
  let timer = null;
  let looking = false;   // a request is in flight; drop frames rather than queue
  let looks = 0;
  // The last frame the model found something in, kept whole: the JPEG it saw,
  // what it said, and where we were. Reporting has to send the picture the
  // finding is about, not whatever happens to be in front of the lens by the
  // time somebody reaches for the button.
  let found = null;
  let here = null;       // {lat, lng, accuracy} once the browser tells us
  let hold = null;       // the countdown on an unreported find, if one is up
  const canvas = document.createElement("canvas");

  toggle.addEventListener("click", () => (stream ? stop("Stopped.") : start()));

  /* --- the sign-in gate -------------------------------------------------

     Reporting mails the report to the person who made it, so there has to be
     one. The camera itself is ungated: seeing the model draw boxes on a real
     road is the demo, and making somebody sign in before they can look at that
     would be a toll booth in front of the interesting part.

     This hides a button. It is not the security boundary -- `POST /api/incidents`
     rejects an unauthenticated call server-side, which is the check that counts. */
  function signedIn() {
    return Boolean(auth?.enabled && auth.user);
  }

  function paintGate() {
    if (!gate) return;
    // Not while the first auth state is still in flight: "signed out" and "not
    // known yet" look the same, and flashing a sign-in panel at somebody who is
    // already signed in reads as a bug.
    if (!authConfigured || !auth?.resolved) {
      gate.hidden = true;
      return;
    }
    gate.hidden = signedIn();
  }

  if (auth) auth.onChange(paintGate);
  paintGate();

  document.querySelector("[data-gate-signin]")?.addEventListener("click", () => {
    auth?.signIn().catch((err) => {
      if (err?.code !== "auth/popup-closed-by-user") fail(err?.message || String(err));
    });
  });

  // Leaving the tab running the camera and the quota down is nobody's intent.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden && stream) stop("Paused — you left the tab.");
  });
  window.addEventListener("pagehide", () => stop(""));

  async function start() {
    // The camera itself is not gated -- watching the model draw boxes on a real
    // road is the demo. But there is no point starting a session that cannot
    // report, so if accounts are configured and nobody is signed in, say so and
    // put the panel up rather than burning quota on looks with no destination.
    if (authConfigured && !signedIn()) {
      paintGate();
      fail("Sign in first — a report goes to your inbox, so it needs an inbox.");
      return;
    }

    errorSlot.hidden = true;
    toggle.disabled = true;
    // A new session reports on what this session finds, so anything held over
    // from the last one goes.
    clearHold();
    found = null;
    reportButton.hidden = true;
    if (shareButton) shareButton.hidden = true;
    status.textContent = "Asking for the camera…";

    try {
      stream = await navigator.mediaDevices.getUserMedia({
        // The back camera on a phone. `ideal` rather than `exact` so a laptop
        // with only a front camera still works instead of throwing.
        video: { facingMode: { ideal: "environment" } },
        audio: false,
      });
    } catch (err) {
      toggle.disabled = false;
      fail(explainCameraFailure(err));
      status.textContent = "Ready.";
      return;
    }

    video.srcObject = stream;
    await video.play().catch(() => {});
    idle.hidden = true;
    toggle.disabled = false;
    toggle.textContent = "Stop";
    status.textContent = "Looking…";
    counter.hidden = false;

    locate();
    look();
    timer = setInterval(look, EVERY_MS);
  }

  /* Where the phone is. Asked for once, alongside the camera.

     A report with no location is not a report -- a road crew cannot act on
     "there is debris somewhere". So this either produces coordinates or says
     out loud that it did not, and the report says the same. It never guesses. */
  function locate() {
    if (!navigator.geolocation) {
      whereSlot.textContent = "This browser will not share a location.";
      whereSlot.hidden = false;
      return;
    }
    whereSlot.textContent = "Getting your location…";
    whereSlot.hidden = false;
    navigator.geolocation.getCurrentPosition(
      (position) => {
        here = {
          lat: position.coords.latitude,
          lng: position.coords.longitude,
          accuracy: Math.round(position.coords.accuracy),
        };
        whereSlot.textContent =
          `${here.lat.toFixed(5)}, ${here.lng.toFixed(5)} (±${here.accuracy}m)`;
      },
      () => {
        here = null;
        whereSlot.textContent =
          "No location — allow it if you want a report a crew can act on.";
      },
      { enableHighAccuracy: true, timeout: 10000, maximumAge: 30000 }
    );
  }

  function stop(message) {
    if (timer) clearInterval(timer);
    timer = null;
    clearHold();
    if (stream) stream.getTracks().forEach((track) => track.stop());
    stream = null;
    video.srcObject = null;
    idle.hidden = false;
    idle.textContent = "Camera off.";
    toggle.textContent = "Start looking";
    toggle.disabled = false;
    if (message) status.textContent = message;
    clearBox();
    // A find does not survive the camera being switched off. Its picture is
    // held in memory here and nowhere else, and offering to report it after the
    // session it belongs to has ended is offering a stale frame.
    found = null;
    reportButton.hidden = true;
    if (shareButton) shareButton.hidden = true;
  }

  async function look() {
    if (looking || !stream) return;
    const jpeg = await capture();
    if (!jpeg) return;

    looking = true;
    try {
      const response = await fetch("/api/dashcam/look", {
        method: "POST",
        headers: { "Content-Type": "image/jpeg" },
        body: jpeg,
      });
      looks += 1;
      counter.textContent = `${looks} look${looks === 1 ? "" : "s"}`;

      if (!response.ok) {
        const detail = await describe(response);
        // Out of quota is the expected ending, not a crash. Say so and stop
        // rather than hammering a throttled endpoint every three seconds.
        if (response.status === 503) {
          stop("Out of model quota for now — give it a minute.");
        }
        fail(detail);
        return;
      }

      show(await response.json(), jpeg);
      errorSlot.hidden = true;
    } catch (err) {
      fail(err.message || "Could not reach the model.");
    } finally {
      looking = false;
    }
  }

  function capture() {
    const width = video.videoWidth;
    const height = video.videoHeight;
    if (!width || !height) return Promise.resolve(null);

    const scale = Math.min(1, SEND_WIDTH / width);
    canvas.width = Math.round(width * scale);
    canvas.height = Math.round(height * scale);
    canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);

    // JPEG, not PNG: the analyzer sends the bytes on with a hardcoded
    // image/jpeg mime type.
    return new Promise((resolve) =>
      canvas.toBlob(resolve, "image/jpeg", JPEG_QUALITY)
    );
  }

  function show(result, jpeg) {
    if (!result.found) {
      clearBox();
      said.textContent = "Nothing worth reporting in that frame.";
      said.hidden = false;
      return;
    }
    drawBox(result);
    said.textContent = `${result.hazard_label} · ${result.confidence.toFixed(2)} — ${result.description}`;
    said.hidden = false;

    found = { result, jpeg, at: new Date(), where: here };
    beginHold();
  }

  /* --- the decision window ----------------------------------------------

     Something has been found, so stop looking and offer it. Two reasons to
     pause rather than carry on: quota, because more calls on a hazard already
     found buy nothing; and the picture, because the report is about the frame
     the model saw, and continuing to look would keep replacing it with whatever
     is in front of the lens by the time somebody reaches for the button.

     Unpressed, the find is dropped and looking resumes. That is the quiet path
     and it must stay quiet -- nothing is uploaded, nothing is stored, no mail. */
  function beginHold() {
    pauseLooking();
    clearHold();

    const until = Date.now() + HOLD_MS;
    reportButton.hidden = false;
    reportButton.disabled = false;
    if (shareButton) shareButton.hidden = false;

    const tick = () => {
      const left = Math.ceil((until - Date.now()) / 1000);
      if (left <= 0) {
        // Nobody wanted it. Drop it whole -- the JPEG included, which is the
        // only copy of that frame anywhere.
        clearHold();
        found = null;
        reportButton.hidden = true;
        if (shareButton) shareButton.hidden = true;
        clearBox();
        said.textContent = "Let that one go. Still looking.";
        resumeLooking();
        return;
      }
      reportButton.textContent = `Report it · ${left}s`;
    };

    tick();
    hold = setInterval(tick, 250);
  }

  function clearHold() {
    if (hold) clearInterval(hold);
    hold = null;
  }

  function pauseLooking() {
    // Only announce the pause if we were actually looking. `beginHold` is also
    // called to *re-arm* the clock after a share or a failed save, and in those
    // cases the status line already says something more useful than the generic
    // prompt -- overwriting it would throw away the confirmation or the error.
    const wasLooking = Boolean(timer);
    if (timer) clearInterval(timer);
    timer = null;
    if (wasLooking) status.textContent = "Found something. Report it, or it goes.";
  }

  /* Start looking again after a find is dealt with.
     `note` replaces the default status line, so a confirmation worth reading --
     "sent to you@example.com" -- is not wiped out by "Looking…" a millisecond
     after it appears. */
  function resumeLooking(note) {
    // Only if the camera is still on. A hold that expires after somebody hit
    // Stop must not quietly start the loop up again.
    if (!stream || timer) return;
    status.textContent = note || "Looking…";
    timer = setInterval(look, EVERY_MS);
  }

  function drawBox(result) {
    if (!result.box) {
      clearBox();
      return;
    }
    let box = boxes.querySelector(".box");
    if (!box) {
      box = document.createElement("div");
      box.className = "box";
      const label = document.createElement("span");
      label.className = "box__label";
      box.appendChild(label);
      boxes.appendChild(box);
    }
    box.hidden = false;
    box.style.left = pct(result.box.x);
    box.style.top = pct(result.box.y);
    box.style.width = pct(result.box.width);
    box.style.height = pct(result.box.height);
    box.classList.toggle("box--soft", !result.box_measured);
    box.querySelector(".box__label").textContent = result.box_label || "";
  }

  function clearBox() {
    boxes.querySelectorAll(".box").forEach((box) => (box.hidden = true));
  }

  // --- reporting -------------------------------------------------------

  reportButton.addEventListener("click", keep);
  shareButton?.addEventListener("click", report);

  /* Keep it: store the finding and mail it to whoever is signed in.
   *
   * This is the only path in this file that uploads anything. It sends the
   * frame the model actually found the hazard in -- with the box burned into
   * the picture, because a rectangle that only exists as a `<div>` on this page
   * is worth nothing in somebody's inbox -- plus what the model said and where
   * the phone was.
   *
   * The server decides who to mail. It reads the address off the verified ID
   * token, never from anything sent here, which is why this request carries no
   * recipient at all.
   */
  async function keep() {
    if (!found) return;

    if (!signedIn()) {
      // Reachable if the token expired while the camera was running.
      paintGate();
      fail("Sign in first — the report goes to your inbox.");
      return;
    }
    if (!found.where) {
      fail(
        "No location for that one, so there is nothing a crew could act on. " +
          "Allow location and try the next find."
      );
      return;
    }

    // Freeze the countdown: this is somebody deciding, and having the find
    // expire out from under an in-flight upload would be absurd.
    clearHold();
    reportButton.disabled = true;
    reportButton.textContent = "Saving…";
    status.textContent = "Working out whose road this is…";

    try {
      const image = await burnBox(found);
      if (!image) {
        fail("Could not render the still.");
        return;
      }

      const form = new FormData();
      form.append(
        "meta",
        JSON.stringify({
          lat: found.where.lat,
          lng: found.where.lng,
          hazard: found.result.hazard,
          severity: found.result.severity,
          confidence: found.result.confidence,
          description: found.result.description,
          model: found.result.model,
          box: found.result.box || null,
          box_measured: Boolean(found.result.box_measured),
        })
      );
      form.append("image", image, "road-hazard.jpg");

      const response = await auth.fetch("/api/incidents", { method: "POST", body: form });
      if (!response.ok) {
        fail(await describe(response));
        return;
      }

      const saved = await response.json();
      errorSlot.hidden = true;
      const note = saved.emailed_to
        ? `Saved, and sent to ${saved.emailed_to}.`
        : "Saved. The mail could not be sent — it is on your incidents page.";

      found = null;
      reportButton.hidden = true;
      if (shareButton) shareButton.hidden = true;
      clearBox();
      status.textContent = note;
      // Carries the confirmation through, rather than replacing it with
      // "Looking…" the instant it appears.
      resumeLooking(note);
    } catch (err) {
      fail(`Could not save that: ${(err && err.message) || err}`);
    } finally {
      reportButton.disabled = false;
      if (found) {
        // It did not save. Put the find back on the clock rather than leaving
        // it pinned and the camera paused indefinitely -- a retry is one press
        // away, and if nobody retries it expires like any other and looking
        // picks back up. `beginHold` writes the button label itself.
        beginHold();
      } else {
        reportButton.textContent = "Report it";
      }
    }
  }

  /* Hand the finding to whatever the phone uses to send things.

     The box is burned into the picture here rather than left as a `<div>`,
     because a rectangle that only exists in the page is worth nothing once the
     image is in somebody's inbox. `navigator.share` with a file opens the
     native share sheet with the image genuinely attached -- pick Mail and it is
     there. `mailto:` cannot attach anything, ever, so on desktop the text goes
     and the picture does not; the draft says so rather than pretending.

     Nothing is sent from here, and nothing is stored. The last action is a
     person's. This is the secondary path -- `keep` is the button, and it saves
     and mails. This one is for handing the picture somewhere yourself. */
  async function report() {
    if (!found) return;
    // Somebody is deciding, so stop the clock. Restored either way in `finally`.
    clearHold();
    if (shareButton) shareButton.disabled = true;
    status.textContent = "Working out whose road this is…";

    // Ask the server who owns the road at these coordinates and how it words a
    // report. The same registry and the same sentences the rest of the system
    // uses -- this is not a second, parallel way of writing a report.
    let addressed = null;
    if (found.where) {
      try {
        const response = await fetch("/api/dashcam/report", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            lat: found.where.lat,
            lng: found.where.lng,
            hazard: found.result.hazard,
            severity: found.result.severity,
            confidence: found.result.confidence,
            description: found.result.description,
            model: found.result.model,
          }),
        });
        if (response.ok) {
          addressed = await response.json();
        } else {
          fail(await describe(response));
        }
      } catch (err) {
        fail(`Could not work out the agency: ${(err && err.message) || err}`);
      }
    }

    const subject = addressed ? addressed.subject : `Road hazard: ${found.result.hazard_label}`;
    const body = addressed ? withFallbackNote(addressed) : reportText(found);
    const to = addressed && addressed.email ? addressed.email : "";

    try {
      const image = await burnBox(found);
      const file = image
        ? new File([image], "road-hazard.jpg", { type: "image/jpeg" })
        : null;

      // The share sheet is the only path that can carry the picture, so it is
      // tried first. Mail is one of its targets, and the draft arrives with the
      // marked still genuinely attached.
      if (file && navigator.canShare?.({ files: [file] })) {
        await navigator.share({ files: [file], title: subject, text: body });
        status.textContent = addressed
          ? `Handed over, addressed to ${addressed.agency}. Sending is still your call.`
          : "Handed to your share sheet — sending is still your call.";
      } else if (to) {
        window.location.href =
          `mailto:${encodeURIComponent(to).replace(/%40/g, "@")}` +
          `?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
        status.textContent =
          `Opened a draft to ${addressed.agency}. Your browser cannot attach the still.`;
      } else if (addressed && addressed.endpoint) {
        // No inbox, so a draft has nowhere to go -- `mailto:` with an empty
        // recipient opens nothing at all. The form is the real channel, and the
        // report goes to the clipboard first so it survives the new tab. There
        // is no report text on this page to fall back on, unlike the case page.
        // `addressed.body`, not `body`: the latter carries the "submit it at
        // <url>" note, which reads as nonsense pasted into that very form.
        const copied = await copyText(addressed.body);
        window.open(addressed.endpoint, "_blank", "noopener");
        status.textContent = copied
          ? `${addressed.agency} takes these by form, not email. Opened it — the report is on your clipboard, ready to paste.`
          : `${addressed.agency} takes these by form, not email. Opened it — the report is above, and your browser blocked the clipboard.`;
      } else {
        fail("No agency resolved for this spot, so there is nowhere to send it.");
      }
    } catch (err) {
      // A cancelled share sheet throws AbortError. That is somebody deciding
      // not to send, which is the whole point of the button, not a failure.
      if (!err || err.name !== "AbortError") {
        fail(`Could not hand that over: ${(err && err.message) || err}`);
      }
    } finally {
      if (shareButton) shareButton.disabled = false;
      // Sharing does not consume the find -- somebody may well want to save it
      // too. Put it back on the clock so it expires like any other rather than
      // pinning the camera indefinitely.
      if (found) beginHold();
    }
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // Insecure context, or permission refused. The status line says so.
      return false;
    }
  }

  /* Most state DOTs publish a form, not an inbox -- they route reports into a
     ticketing system on purpose. This note rides along on the share-sheet path,
     where the report lands somewhere with no recipient of its own and the URL
     is the only thing saying where it needs to go. The form path strips it:
     see `report`, which copies the bare body instead. */
  function withFallbackNote(addressed) {
    if (addressed.email) return addressed.body;
    return [
      addressed.body,
      "",
      `${addressed.agency} does not publish an email for this. Submit it at:`,
      addressed.endpoint || "their website",
    ].join("\n");
  }

  /* The report, written the same way the rest of the system writes one:
     what, where, when, and no claim that is not true. */
  function reportText(finding) {
    const r = finding.result;
    const where = finding.where
      ? `${finding.where.lat.toFixed(5)}, ${finding.where.lng.toFixed(5)} ` +
        `(to within about ${finding.where.accuracy} m)`
      : "not recorded — the browser did not share a location";

    return [
      "Reporting a road hazard seen from a vehicle dashcam.",
      "",
      `Location: ${where}`,
      `Observed: ${finding.at.toLocaleString()}`,
      "",
      r.description,
      "",
      `Identified as ${r.hazard_label} at ${r.confidence.toFixed(2)} confidence ` +
        `by ${r.model}, from a still taken at the moment above.`,
      "",
      "Spotted by Road Cleaner. Sent by a person who saw it.",
    ].join("\n");
  }

  /* Draw the detection box onto a copy of the frame the model actually saw. */
  function burnBox(finding) {
    return new Promise((resolve) => {
      const box = finding.result.box;
      const image = new Image();
      image.onload = () => {
        const out = document.createElement("canvas");
        out.width = image.width;
        out.height = image.height;
        const ctx = out.getContext("2d");
        ctx.drawImage(image, 0, 0);

        if (box) {
          const stroke = Math.max(3, Math.round(image.width * 0.005));
          ctx.strokeStyle = "#E2622B";
          ctx.lineWidth = stroke;
          ctx.strokeRect(
            box.x * image.width, box.y * image.height,
            box.width * image.width, box.height * image.height
          );
          const label = finding.result.box_label || "";
          if (label) {
            const size = Math.max(14, Math.round(image.width * 0.022));
            ctx.font = `600 ${size}px system-ui, sans-serif`;
            const pad = Math.round(size * 0.35);
            const width = ctx.measureText(label).width + pad * 2;
            const top = Math.max(0, box.y * image.height - size - pad * 2);
            ctx.fillStyle = "#E2622B";
            ctx.fillRect(box.x * image.width, top, width, size + pad * 2);
            ctx.fillStyle = "#fff";
            ctx.fillText(label, box.x * image.width + pad, top + size + pad * 0.4);
          }
        }
        out.toBlob(resolve, "image/jpeg", 0.9);
      };
      image.onerror = () => resolve(null);
      image.src = URL.createObjectURL(finding.jpeg);
    });
  }

  function explainCameraFailure(err) {
    // `getUserMedia` is only available in a secure context, which catches
    // everyone testing over a LAN address rather than localhost or the deployed
    // HTTPS URL. That is worth naming rather than reporting as "undefined".
    if (!window.isSecureContext) {
      return "The camera needs a secure page — open this over https, or on localhost.";
    }
    if (err && err.name === "NotAllowedError") {
      return "Camera permission was refused. Allow it in your browser settings and try again.";
    }
    if (err && err.name === "NotFoundError") {
      return "No camera found on this device.";
    }
    return `Could not open the camera: ${(err && err.message) || err}`;
  }

  function fail(message) {
    errorSlot.textContent = message;
    errorSlot.hidden = false;
  }

  async function describe(response) {
    try {
      const body = await response.json();
      return body.detail || `HTTP ${response.status}`;
    } catch {
      return `HTTP ${response.status}`;
    }
  }

  const pct = (fraction) => `${(fraction * 100).toFixed(1)}%`;
})();
