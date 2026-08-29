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
 *    them a minute before it throttles. So: a deliberate gap between *starting*
 *    looks, and a counter on screen. A demo that dies mysteriously at look
 *    twenty-two is worse than one that tells you it is at look nineteen.
 * 2. Looking stops the moment something is found. A hazard is one object, and
 *    spending more quota on the thing you have already seen is waste. The find
 *    is held for a fixed window -- see `hold` -- and then dropped.
 * 3. **A frame is only kept if you press the button.** Every frame goes to the
 *    model and is discarded. The one exception is a find you actively report:
 *    that still is stored and mailed to you, and it is the only thing here that
 *    ever leaves the browser to be written down. A find that times out unpressed
 *    leaves nothing behind at all -- no record, no mail, nothing uploaded.
 *
 * Looks overlap, and that is the whole of why this page keeps up.
 *
 * It used to run one request at a time on a three-second timer, which sounds
 * like a look every three seconds and is not: the timer could only start a look
 * when the previous one had come back, so a five-second round trip made it a
 * look every eight. Latency set the pace, and every slow frame was paid for
 * twice.
 *
 * Now the gap and the round trip are independent. `pump` starts a look whenever
 * fewer than MAX_IN_FLIGHT are outstanding and MIN_GAP_MS has passed since the
 * last one started, so the gap alone decides how much quota is spent and the
 * latency hides behind it. Overlapping means answers can arrive out of order, so
 * every look carries a sequence number and one that comes back after a newer one
 * has already been drawn is dropped -- see `look`. Without that the boxes would
 * jump backwards onto a road that has moved on.
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

  // The find dialog. Every one of these may be absent -- the page renders
  // without it in older browsers -- so everything that touches them is guarded.
  const findModal = document.getElementById("find-modal");
  const findImage = document.getElementById("find-image");
  const findOverlay = document.getElementById("find-overlay");
  const findLabel = document.getElementById("find-label");
  const findTitle = document.getElementById("find-title");
  const findEyebrow = document.getElementById("find-eyebrow");
  const findDescription = document.getElementById("find-description");
  const findWhere = document.getElementById("find-where");
  const findReport = document.getElementById("find-report");
  const findShare = document.getElementById("find-share");
  const findDismiss = document.getElementById("find-dismiss");
  // The object URL behind the dialog's <img>, revoked when it closes. Without
  // this every find leaks a blob for the life of the tab.
  let findImageUrl = null;

  const auth = window.RoadCleaner?.auth;
  const authConfigured = root.dataset.auth === "on";

  /* Pacing. Server-supplied so the quota knob can be turned for a deployment
     without editing this file; see the comments in `config.py`.

     MIN_GAP_MS is the quota control -- it is the only thing that decides how
     many calls a minute this makes. MAX_IN_FLIGHT is a pile-up control: it caps
     how many slow looks may be outstanding at once, and raising it spends no
     extra quota because the gap still governs how often one starts.

     PUMP_MS is just how often the scheduler wakes to check those two. It is
     deliberately much shorter than the gap so that a slot freeing up early is
     noticed promptly rather than at the next multiple of the gap. */
  const MIN_GAP_MS = Number(root.dataset.lookGap) || 2500;
  const MAX_IN_FLIGHT = Math.max(1, Number(root.dataset.maxInFlight) || 3);
  const PUMP_MS = 250;

  // How long the browser waits for one look before abandoning it. The server
  // applies its own, slightly shorter deadline and answers 504; this is the
  // backstop for the case where the answer never arrives at all, which is what
  // a phone dropping off the network looks like from in here. Without it an
  // abandoned request would hold one of the MAX_IN_FLIGHT slots for ever.
  const LOOK_TIMEOUT_MS = Number(root.dataset.lookTimeout) || 11000;

  // Whether the camera may start at all without location. See
  // DASHCAM_REQUIRE_LOCATION -- the short version is that a find with no
  // coordinates cannot be filed, so a session without them wastes both quota and
  // somebody's attention.
  const requireLocation = root.dataset.requireLocation !== "off";

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
  let timer = null;      // the pump, when looking is live
  let inFlight = 0;      // looks currently waiting on the model
  let lastLaunch = 0;    // when the most recent one started, for MIN_GAP_MS
  let looks = 0;         // looks *started*, which is what the quota counts
  let skipped = 0;       // ones the deadline caught, shown as a tally not an alarm
  let nextSeq = 1;       // stamped on each look, to order the answers
  let newestDrawn = 0;   // the sequence number of the freshest answer drawn
  // The last frame the model found something in, kept whole: the JPEG it saw,
  // what it said, and where we were. Reporting has to send the picture the
  // finding is about, not whatever happens to be in front of the lens by the
  // time somebody reaches for the button.
  let found = null;
  let here = null;       // {lat, lng, accuracy} once the browser tells us
  let watch = null;      // the watchPosition id, while one is running
  let hold = null;       // the countdown on an unreported find, if one is up
  const pending = new Set();  // AbortControllers for the looks still out

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

    /* Location first, and before the camera on purpose.

       It used to run after `getUserMedia` had resolved, and on a phone the
       prompt frequently never appeared at all -- the camera prompt was answered
       and the location one simply never showed, so a session could run for
       minutes with every find coming out unreportable. The likeliest reading is
       that a second permission request arriving as the first is dismissed gets
       dropped rather than queued; what is certain is that moving it in front of
       the camera made the prompt appear reliably.

       Asking first also means the tap that started this is still the most recent
       user gesture, which is what a browser wants to see before it will prompt
       for something like this at all. */
    if (requireLocation) {
      status.textContent = "Asking for your location…";
      const verdict = await ensureLocationPermission();
      if (!verdict.ok) {
        toggle.disabled = false;
        status.textContent = "Ready.";
        fail(verdict.reason);
        showWhere(verdict.short);
        return;
      }
    }

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
    // Straight into the first look rather than waiting out a gap for it.
    lastLaunch = 0;
    timer = setInterval(pump, PUMP_MS);
    pump();
  }

  /* The scheduler. Starts a look when there is room for one and enough time has
     passed since the last one started.

     Both conditions, and they do different jobs: the gap is what keeps the call
     rate inside the quota, and the in-flight ceiling is what stops a run of slow
     answers from queueing up behind each other. Neither alone is enough. */
  function pump() {
    if (!stream || !timer) return;
    // A find pauses looking, and `found` is set before `pauseLooking` clears the
    // timer, so this closes the window where a pump tick could start one more
    // look at the very moment something was found.
    if (found) return;
    if (inFlight >= MAX_IN_FLIGHT) return;
    if (Date.now() - lastLaunch < MIN_GAP_MS) return;

    lastLaunch = Date.now();
    // Not awaited: the pump's job is to start looks, not to wait for them. That
    // is the entire point of the change -- a slow answer must not delay the next
    // question. `look` reports its own failures.
    void look(nextSeq++);
  }

  /* --- where the phone is -----------------------------------------------

     A report with no location is not a report -- a road crew cannot act on
     "there is debris somewhere". So this either produces coordinates or says
     out loud that it did not, and the report says the same. It never guesses.

     Two requests, not one, because they are asking different questions.

     `getCurrentPosition` with `enableHighAccuracy` used to be the whole of it,
     with a ten-second timeout. On a phone that is a coin flip: a cold GPS fix
     regularly takes longer than ten seconds, and when it did, `here` stayed null
     for the rest of the session. Every find after that was unreportable, and the
     only sign of it was one line of grey text near the top of the page.

     So: a coarse fix first, which the network can usually answer in a moment and
     is easily good enough to name a road, and then a `watchPosition` that
     refines it and keeps refining. A watch is the right primitive anyway -- this
     is a camera in a moving vehicle, and a position from the start of the session
     describes a place the car has since left. */
  /* Get location permission settled before the camera opens.

     Resolves `{ok: true}` once the browser will give us positions, or
     `{ok: false, reason, short}` when it will not and no amount of waiting
     changes that.

     Gates on permission, never on a fix. A first GPS lock outdoors can take
     twenty seconds and indoors may never come; refusing to open the camera over
     that would be a worse bug than the one this fixes. What it refuses is a
     session that is *never* going to produce a location -- a blocked site, a
     browser without geolocation, an insecure page. Once permission is granted
     the watch fills the position in whenever it arrives, and the report buttons
     stay disabled until it does. */
  async function ensureLocationPermission() {
    if (!navigator.geolocation) {
      return {
        ok: false,
        short: "This browser will not share a location.",
        reason:
          "This browser will not share a location, and a report without one " +
          "cannot be sent to a crew.",
      };
    }
    if (!window.isSecureContext) {
      return {
        ok: false,
        short: "Location needs a secure page — open this over https.",
        reason:
          "Location needs a secure page. Open this over https, or on localhost.",
      };
    }

    // Asked first where it is supported, because it is the only way to tell
    // "blocked" from "not asked yet" *without* firing a prompt. A blocked site
    // fails `getCurrentPosition` instantly and silently, which is exactly what
    // looks like "I never saw the request".
    let state = null;
    try {
      // Not `status` -- that is the page's status line, and shadowing it here
      // would be a trap for the next person to add a line to this block.
      const permission = await navigator.permissions?.query({ name: "geolocation" });
      state = permission?.state ?? null;
    } catch {
      // Safari has historically thrown on this query rather than resolving.
      // Not knowing is fine -- fall through and let the prompt decide.
      state = null;
    }

    if (state === "denied") return blockedVerdict();
    if (state === "granted") {
      locate();
      return { ok: true };
    }

    // Either "prompt" or unknown. This call is what actually raises the dialog,
    // and it is deliberately the first thing this page asks for.
    const granted = await new Promise((resolve) => {
      navigator.geolocation.getCurrentPosition(
        (position) => {
          gotPosition(position);
          resolve(true);
        },
        (error) => resolve(error?.code !== 1 /* PERMISSION_DENIED */),
        // Generous, because this is a real first fix and the alternative is
        // telling somebody they are blocked when they simply had not answered
        // yet. A timeout is not a refusal: it resolves true and the watch
        // carries on trying.
        { enableHighAccuracy: false, timeout: 25000, maximumAge: 60000 }
      );
    });

    if (!granted) return blockedVerdict();
    locate();
    return { ok: true };
  }

  function blockedVerdict() {
    return {
      ok: false,
      short: "Location is blocked for this site.",
      reason:
        "Location is blocked for this site, so a find here could never be " +
        "reported. On iPhone: tap the ⚙ or “aA” in the address bar → Website " +
        "Settings → Location → Ask or Allow, then press Start again. If it is " +
        "off for Safari everywhere, it is Settings → Privacy & Security → " +
        "Location Services → Safari Websites.",
    };
  }

  function locate() {
    if (!navigator.geolocation) {
      showWhere("This browser will not share a location.");
      return;
    }
    // Geolocation is a secure-context API, exactly like the camera. Over plain
    // http on a LAN address it fails with a bare POSITION_UNAVAILABLE, which
    // reads as a hardware problem and is not one.
    if (!window.isSecureContext) {
      showWhere("Location needs a secure page — open this over https.");
      return;
    }

    stopWatching();

    // The quick one, skipped when the permission gate has just produced a fix of
    // its own -- there is no sense asking a phone where it is twice in the same
    // second, and the watch below keeps it current from here.
    if (here) {
      showWhere(`${here.lat.toFixed(5)}, ${here.lng.toFixed(5)} (±${here.accuracy}m)`);
    } else {
      showWhere("Getting your location…");
      // `maximumAge` accepts a fix the browser already had, which on a phone
      // that has been navigating is usually instant.
      navigator.geolocation.getCurrentPosition(gotPosition, noPosition, {
        enableHighAccuracy: false,
        timeout: 20000,
        maximumAge: 60000,
      });
    }

    // The good one, kept running. Its errors are deliberately ignored: the
    // coarse fix above owns the error message, and a watch that cannot get a
    // high-accuracy fix while a usable coarse one is on screen has nothing to
    // add by overwriting it.
    watch = navigator.geolocation.watchPosition(gotPosition, () => {}, {
      enableHighAccuracy: true,
      timeout: 30000,
      maximumAge: 10000,
    });
  }

  function gotPosition(position) {
    here = {
      lat: position.coords.latitude,
      lng: position.coords.longitude,
      accuracy: Math.round(position.coords.accuracy),
    };
    showWhere(`${here.lat.toFixed(5)}, ${here.lng.toFixed(5)} (±${here.accuracy}m)`);
    // A find made before the fix arrived is still reportable -- see `keep`. Now
    // that there is somewhere to send it, the button should stop saying there
    // is not.
    if (found) paintReportButton();
  }

  /* Why there is no location, in words that match what actually happened.

     This used to be one sentence for all three causes: "allow it if you want a
     report a crew can act on". Told to somebody who had already allowed it and
     was simply waiting on a fix, that is not just unhelpful, it is wrong -- it
     sends them to settings to check something that was never the problem. */
  function noPosition(error) {
    here = null;
    const code = error && error.code;
    if (code === 1 /* PERMISSION_DENIED */) {
      showWhere("Location is blocked. Allow it for this site, then tap to retry.");
    } else if (code === 3 /* TIMEOUT */) {
      showWhere("Still no fix — a first one can take a while. Tap to retry.");
    } else {
      showWhere("Your phone could not get a fix here. Tap to retry.");
    }
    if (found) paintReportButton();
  }

  function showWhere(text) {
    whereSlot.textContent = text;
    whereSlot.hidden = false;
  }

  function stopWatching() {
    if (watch !== null) navigator.geolocation.clearWatch(watch);
    watch = null;
  }

  /* Tapping either location line asks again. A refused or timed-out fix is the
     one failure on this page somebody can actually do something about, so it
     must not need the camera stopped and restarted to have another go.

     Both lines, and the second one is not redundant: a modal dialog sits in the
     browser's top layer, so while a find is up the line on the page behind it
     cannot be tapped at all. Driving this in a browser is how that turned up --
     the retry existed and was unreachable at the exact moment somebody would
     want it, which is while they are looking at a find they cannot report. */
  whereSlot?.addEventListener("click", retryLocation);
  findWhere?.addEventListener("click", retryLocation);

  async function retryLocation() {
    if (stream) {
      locate();
      return;
    }
    // No camera running, which with DASHCAM_REQUIRE_LOCATION on usually means
    // Start was refused for exactly this reason. Re-check rather than doing
    // nothing: somebody who has just been told to change a Safari setting will
    // come back and tap this, and the answer may well be different now.
    if (!requireLocation) return;
    showWhere("Checking…");
    const verdict = await ensureLocationPermission();
    if (verdict.ok) {
      errorSlot.hidden = true;
      showWhere("Location allowed. Press Start looking.");
    } else {
      showWhere(verdict.short);
      fail(verdict.reason);
    }
  }

  function stop(message) {
    if (timer) clearInterval(timer);
    timer = null;
    clearHold();
    // Abandon anything still out at the model. With looks overlapping there can
    // be several, and every one of them is a question about a road this session
    // is no longer watching -- letting them land would draw a box over a stopped
    // camera, or worse, offer a stale find to report.
    abandonLooks();
    // Let the GPS go too. A watch left running after Stop keeps the receiver
    // awake and the location indicator lit, on a page that has finished with
    // both -- the same reason the camera tracks are stopped just below.
    stopWatching();
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
    closeFind();
    reportButton.hidden = true;
    if (shareButton) shareButton.hidden = true;
  }

  /* One look, stamped with `seq` so its answer can be placed in time.

     Everything that reports progress counts looks *started*, not looks that came
     back. A run of failures used to leave the counter frozen at whatever the
     last success was, which reads exactly like the page has stopped -- and the
     page had not stopped, it was retrying every three seconds and saying so
     nowhere. The number on screen is the number of calls made, because that is
     the number the quota is counting too. */
  async function look(seq) {
    if (!stream) return;
    const jpeg = await capture();
    // Checked again on the far side of the await: encoding a frame takes a
    // moment, and Stop may have happened during it. Without this a look started
    // just before Stop would go on to open a request the session no longer
    // wants, and `abandonLooks` has already been and gone by then.
    if (!jpeg || !stream) return;

    inFlight += 1;
    looks += 1;
    paintCounter();

    // A request the browser gives up on, rather than one that hangs holding a
    // slot until the camera is switched off. Registered so `abandonLooks` can
    // cut it short too.
    const abort = new AbortController();
    const deadline = setTimeout(() => abort.abort(), LOOK_TIMEOUT_MS);
    pending.add(abort);

    try {
      const response = await fetch("/api/dashcam/look", {
        method: "POST",
        headers: { "Content-Type": "image/jpeg" },
        body: jpeg,
        signal: abort.signal,
      });

      if (!response.ok) {
        const detail = await describe(response);
        // Out of quota is the expected ending, not a crash. Say so and stop
        // rather than hammering a throttled endpoint.
        if (response.status === 503) {
          stop("Out of model quota for now — give it a minute.");
          fail(detail);
          return;
        }
        // 504 is one slow frame, not a broken page: the server gave up on it so
        // the road would not get further away while it waited. Counted, not
        // announced -- several other looks are already in the air, and the next
        // answer is usually only a second or two behind.
        if (response.status === 504) {
          skipped += 1;
          paintCounter();
          return;
        }
        fail(detail);
        return;
      }

      // Overlapping looks can answer out of order. An answer older than one
      // already on screen describes a road that has since moved, so it is
      // dropped rather than drawn -- otherwise the box jumps backwards.
      if (seq < newestDrawn) return;
      newestDrawn = seq;

      const result = await response.json();
      // The camera may have been switched off, or something else found, while
      // this was in the air. Either way there is nothing left to draw on.
      if (!stream || found) return;

      show(result, jpeg);
      errorSlot.hidden = true;
    } catch (err) {
      // An abort is either the deadline above or `abandonLooks` on the way out
      // of a session. Neither deserves an error box: the first is one skipped
      // frame, the second is somebody having pressed Stop.
      if (err && err.name === "AbortError") {
        if (stream) {
          skipped += 1;
          paintCounter();
        }
      } else {
        fail((err && err.message) || "Could not reach the model.");
      }
    } finally {
      clearTimeout(deadline);
      pending.delete(abort);
      inFlight -= 1;
      paintCounter();
    }
  }

  /* Give up on every look still out at the model. Called when a session ends, so
     that answers about a road nobody is watching any more cannot land.

     `inFlight` is deliberately not zeroed here. Each aborted fetch still runs its
     own `finally`, which decrements it -- and one of those aborts is often the
     look that called `stop` in the first place, on its way through a 503. Zeroing
     as well would double-count every one of them and leave the counter negative,
     which the next session would read as room for extra concurrent looks. */
  function abandonLooks() {
    pending.forEach((abort) => abort.abort());
    pending.clear();
  }

  function paintCounter() {
    const waiting = inFlight > 0 ? ` · ${inFlight} in flight` : "";
    // Skips belong here, as a tally, not on the status line. They are ordinary
    // -- the model has a long tail and several looks are always in the air -- and
    // a sentence that replaces "Looking…" every few seconds reads as a broken
    // page rather than as one frame of many going unanswered.
    const slow = skipped > 0 ? ` · ${skipped} slow` : "";
    counter.textContent = `${looks} look${looks === 1 ? "" : "s"}${waiting}${slow}`;
  }

  function capture() {
    const width = video.videoWidth;
    const height = video.videoHeight;
    if (!width || !height) return Promise.resolve(null);

    // A canvas per capture, not one shared between them. `toBlob` reads the
    // bitmap asynchronously, so with looks overlapping a shared canvas could be
    // redrawn by the next capture before the previous one had finished encoding
    // -- and the model would be asked about one frame while being sent another.
    const canvas = document.createElement("canvas");
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

  /* Where the find happened, as well as it can be known.

     `found.where` is the fix as it stood when the model answered, which is the
     honest one: it is where the phone was when it saw the thing. But a first GPS
     fix often lands *after* the first find, and the old code snapshotted null and
     kept it -- so a find made in the first few seconds of a session stayed
     permanently unreportable even once the phone knew exactly where it was.

     So a fix that arrived late is used rather than discarded. The car has moved
     a little in the seconds since, which is well inside the margin that already
     applies to a hazard seen from a moving vehicle, and enormously better than
     refusing to file a real hazard over it. */
  function placeOf(finding) {
    return finding.where || here;
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
    openFind();

    const tick = () => {
      const left = Math.ceil((until - Date.now()) / 1000);
      if (left <= 0) {
        // Nobody wanted it. Drop it whole -- the JPEG included, which is the
        // only copy of that frame anywhere.
        clearHold();
        found = null;
        closeFind();
        reportButton.hidden = true;
        if (shareButton) shareButton.hidden = true;
        clearBox();
        said.textContent = "Let that one go. Still looking.";
        resumeLooking();
        return;
      }
      paintReportButton(left);
    };

    tick();
    hold = setInterval(tick, 250);
  }

  /* --- the find dialog ---------------------------------------------------

     The buttons in the bar are easy to miss on a phone held up to a windscreen,
     and a find is gone in {HOLD_MS} either way. So the find is put in front of
     you: the frame the model saw, the box on it, and the one button that matters.

     Guarded throughout on `findModal` and on `showModal` being a function. The
     dialog element is well supported now but not universally, and where it is
     missing the page still works exactly as it did -- the bar buttons are shown
     and driven the same way regardless of whether this dialog ever opens. */
  function findDialogWorks() {
    return Boolean(findModal && typeof findModal.showModal === "function");
  }

  function openFind() {
    if (!findDialogWorks() || !found || findModal.open) return;
    const result = found.result;

    if (findTitle) findTitle.textContent = result.hazard_label || "A hazard";
    if (findEyebrow) {
      findEyebrow.textContent =
        `Found something · ${result.confidence.toFixed(2)} confidence`;
    }
    if (findDescription) findDescription.textContent = result.description || "";

    // The frame the model actually looked at, not the live preview behind it.
    if (findImage) {
      if (findImageUrl) URL.revokeObjectURL(findImageUrl);
      findImageUrl = URL.createObjectURL(found.jpeg);
      findImage.src = findImageUrl;
      findImage.alt = `${result.hazard_label || "A hazard"}, as the model saw it.`;
    }
    // Positioned as percentages of the image, exactly as on the live preview and
    // on the incidents page. The same fractions drive all three.
    if (findOverlay) {
      if (result.box) {
        findOverlay.hidden = false;
        findOverlay.style.left = pct(result.box.x);
        findOverlay.style.top = pct(result.box.y);
        findOverlay.style.width = pct(result.box.width);
        findOverlay.style.height = pct(result.box.height);
        findOverlay.classList.toggle("find__box-overlay--soft", !result.box_measured);
        if (findLabel) findLabel.textContent = result.box_label || "";
      } else {
        findOverlay.hidden = true;
      }
    }

    paintFindWhere();
    findModal.showModal();
  }

  function closeFind() {
    if (!findDialogWorks()) return;
    if (findModal.open) findModal.close();
    if (findImageUrl) {
      URL.revokeObjectURL(findImageUrl);
      findImageUrl = null;
    }
  }

  /* Whether this find can actually be filed, said before the button is pressed.

     This is the line the whole location fix is for. Reporting needs coordinates,
     and somebody who is about to lose a find in ten seconds needs to know that
     *now* -- not by pressing a button that appears to work and then does nothing
     except put a sentence in a box further down the page. */
  function paintFindWhere() {
    if (!findWhere) return;
    const place = found ? placeOf(found) : null;
    if (place) {
      findWhere.textContent =
        `${place.lat.toFixed(5)}, ${place.lng.toFixed(5)} (±${place.accuracy}m)`;
      findWhere.className = "find__where";
    } else {
      findWhere.textContent =
        "No location yet — a crew cannot be sent to a report without one. " +
        "Allow location, or tap here to try again.";
      findWhere.className = "find__where find__where--missing";
    }
  }

  /* The report buttons, in the bar and in the dialog, kept saying the same thing.

     Both carry the countdown, and both go flat when there is nowhere to send the
     report. A button that looks live and then refuses is the bug that started
     all of this. */
  function paintReportButton(secondsLeft) {
    // No find: back to the resting label, live. Both buttons are hidden or the
    // dialog is closed by the time this happens, but leaving a flat "No
    // location" button behind would be the state the *next* find inherits.
    if (!found) {
      reportButton.textContent = "Report it";
      reportButton.disabled = false;
      if (findReport) {
        findReport.textContent = "Report it now";
        findReport.disabled = false;
      }
      return;
    }

    const place = placeOf(found);
    const suffix = secondsLeft === undefined ? "" : ` · ${secondsLeft}s`;

    reportButton.textContent = place
      ? `Report it${suffix}`
      : `No location${suffix}`;
    reportButton.disabled = !place;

    if (findReport) {
      findReport.textContent = place
        ? `Report it now${suffix}`
        : `Waiting for location${suffix}`;
      findReport.disabled = !place;
    }
    paintFindWhere();
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
     `message` replaces the default status line, so a confirmation worth reading
     -- "sent to you@example.com" -- is not wiped out by "Looking…" a
     millisecond after it appears. */
  function resumeLooking(message) {
    // Only if the camera is still on. A hold that expires after somebody hit
    // Stop must not quietly start the loop up again.
    if (!stream || timer) return;
    status.textContent = message || "Looking…";
    // Straight back to looking rather than sitting out a gap first: the pause
    // was somebody reading a find, and the quota was not being spent during it.
    lastLaunch = 0;
    timer = setInterval(pump, PUMP_MS);
    pump();
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

  // The dialog's buttons are the same two actions, plus letting the find go
  // early. `keep` and `report` close the dialog themselves once they know what
  // happened, so that a confirmation is not hidden the instant it is written.
  findReport?.addEventListener("click", keep);
  findShare?.addEventListener("click", report);
  findDismiss?.addEventListener("click", dropFind);

  /* Escape means the same thing as pressing "Let it go".

     `preventDefault` then `dropFind` rather than letting the default close run,
     because the two are not the same: the default would hide the dialog and
     leave the find alive behind it, counting down invisibly with the camera
     still paused. Dismissing the panel has to dismiss the thing it is about.

     Only Escape, deliberately. A tap on the backdrop is not wired up -- native
     dialogs do not close on one unless asked to, and here that default is the
     right one. This panel is holding something that expires in seconds, and a
     stray thumb on the way to the Report button should not throw it away. */
  findModal?.addEventListener("cancel", (event) => {
    event.preventDefault();
    dropFind();
  });

  /* Let the find go now rather than waiting out its countdown. Deliberately the
     same path the countdown takes: the frame is dropped whole, nothing is
     uploaded, and looking picks back up. */
  function dropFind() {
    if (!found) {
      closeFind();
      return;
    }
    clearHold();
    found = null;
    closeFind();
    reportButton.hidden = true;
    if (shareButton) shareButton.hidden = true;
    clearBox();
    said.textContent = "Let that one go. Still looking.";
    resumeLooking();
  }

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
      closeFind();
      fail("Sign in first — the report goes to your inbox.");
      return;
    }
    // A fix that arrived after the find counts -- see `placeOf`. The buttons are
    // disabled while this is null, so reaching here means somebody got past a
    // flat button; it stays as a guard rather than a message, because by now the
    // dialog has been saying there is no location for as long as it has been up.
    const place = placeOf(found);
    if (!place) {
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
    if (findReport) {
      findReport.disabled = true;
      findReport.textContent = "Saving…";
    }
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
          lat: place.lat,
          lng: place.lng,
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
      // Three outcomes, and the middle one is the one worth being clear about:
      // nothing was mailed, but that is because the road already got reported,
      // not because anything went wrong. Somebody who stopped to report a
      // hazard deserves to be told it is already in hand.
      let note;
      if (saved.dedup_reason) {
        note =
          `Saved — that makes ${saved.reports_24h} reports of this in ` +
          `${saved.dedup_window_hours}h, so it is already in hand and ` +
          "nothing was sent again.";
      } else if (saved.emailed_to) {
        note = `Saved, and sent to ${saved.emailed_to}.`;
      } else {
        note = "Saved. The mail could not be sent — it is on your incidents page.";
      }

      found = null;
      // Saved, so the dialog has done its job and the confirmation belongs on
      // the page behind it rather than under a panel somebody has to dismiss.
      closeFind();
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
        // picks back up. `beginHold` writes both button labels itself.
        beginHold();
      } else {
        paintReportButton();
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
    // Sharing works without coordinates -- it produces a report that says out
    // loud that the location was not recorded, which is a thing a person can
    // still usefully hand to somebody. Only the agency lookup needs a position,
    // so only that part is skipped.
    const shared = placeOf(found);
    if (shared) {
      try {
        const response = await fetch("/api/dashcam/report", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            lat: shared.lat,
            lng: shared.lng,
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
    const place = placeOf(finding);
    const where = place
      ? `${place.lat.toFixed(5)}, ${place.lng.toFixed(5)} ` +
        `(to within about ${place.accuracy} m)`
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
