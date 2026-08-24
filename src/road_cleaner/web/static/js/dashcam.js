/* The agent, pointed at a real road.
 *
 * Everywhere else on this site the model reads a generated clip or a public
 * traffic camera. Here it reads whatever is in front of your phone, and the
 * boxes land on real objects on a real road. Same prompt, same model, same
 * drawing code -- the only thing that changed is where the pixels come from.
 *
 * Two constraints shape all of it:
 *
 * 1. Quota. Each look is a Vertex call and there are only twenty or thirty of
 *    them before it throttles. So: one request in flight at a time, a deliberate
 *    gap between looks, and a counter on screen. A demo that dies mysteriously
 *    at look twenty-two is worse than one that tells you it is at look nineteen.
 * 2. Nothing is kept. The server writes no frame, opens no case and files
 *    nothing. This file never asks it to.
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

  // Long enough that a minute of looking costs about twenty calls rather than
  // sixty, short enough that the boxes still feel attached to the road.
  const EVERY_MS = 3000;

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
  const canvas = document.createElement("canvas");

  toggle.addEventListener("click", () => (stream ? stop("Stopped.") : start()));

  // Leaving the tab running the camera and the quota down is nobody's intent.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden && stream) stop("Paused — you left the tab.");
  });
  window.addEventListener("pagehide", () => stop(""));

  async function start() {
    errorSlot.hidden = true;
    toggle.disabled = true;
    // A new session reports on what this session finds. The previous find stays
    // available right up until then, because driving past a hazard means the
    // next frame is empty and that is when somebody reaches for the button.
    found = null;
    reportButton.hidden = true;
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
    if (stream) stream.getTracks().forEach((track) => track.stop());
    stream = null;
    video.srcObject = null;
    idle.hidden = false;
    idle.textContent = "Camera off.";
    toggle.textContent = "Start looking";
    toggle.disabled = false;
    if (message) status.textContent = message;
    clearBox();
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
      // The previous find stays reportable. Driving past a hazard means the
      // next frame is empty, and that is exactly when somebody reaches for the
      // button.
      return;
    }
    drawBox(result);
    said.textContent = `${result.hazard_label} · ${result.confidence.toFixed(2)} — ${result.description}`;
    said.hidden = false;

    found = { result, jpeg, at: new Date(), where: here };
    reportButton.hidden = false;
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

  reportButton.addEventListener("click", report);

  /* Hand the finding to whatever the phone uses to send things.

     The box is burned into the picture here rather than left as a `<div>`,
     because a rectangle that only exists in the page is worth nothing once the
     image is in somebody's inbox. `navigator.share` with a file opens the
     native share sheet with the image genuinely attached -- pick Mail and it is
     there. `mailto:` cannot attach anything, ever, so on desktop the text goes
     and the picture does not; the draft says so rather than pretending.

     Nothing is sent from here. The last action is a person's. */
  async function report() {
    if (!found) return;
    reportButton.disabled = true;
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
      } else {
        window.location.href =
          `mailto:${encodeURIComponent(to)}` +
          `?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
        status.textContent = to
          ? `Opened a draft to ${addressed.agency}. Your browser cannot attach the still.`
          : "Opened a draft. Your browser cannot attach the still to it.";
      }
    } catch (err) {
      // A cancelled share sheet throws AbortError. That is somebody deciding
      // not to send, which is the whole point of the button, not a failure.
      if (!err || err.name !== "AbortError") {
        fail(`Could not hand that over: ${(err && err.message) || err}`);
      }
    } finally {
      reportButton.disabled = false;
    }
  }

  /* Most state DOTs publish a form, not an inbox -- they route reports into a
     ticketing system on purpose. Where there is no address to send to, the draft
     still opens with everything written, and says where it needs to go. */
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
